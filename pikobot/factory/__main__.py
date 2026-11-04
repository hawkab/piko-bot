"""Machine event CLI backed by the actual pikobot AgentRunner."""
import argparse
import hashlib
import asyncio
import json
import sys
import uuid

from pikobot.agent.runner import AgentRunner, AgentRunSpec
from pikobot.agent.hook import AgentHook
from pikobot.providers.base import ToolCallRequest, ContextWindowExceeded
from .journal import Journal, loads
from .provider import FactoryProvider, TIMING_FIELDS
from .tools import FactoryTools, MCP
from .progress import check as check_progress, StalledToolProgress


def emit(value):
    print(json.dumps(value, ensure_ascii=False, allow_nan=False), flush=True)


class DurableRunner(AgentRunner):
    async def prepare_context(self, spec):
        """Fetch the mandatory scoped packet once, without model inference."""
        registry = spec.tools
        journal, turn = registry.journal, registry.turn
        previous = [r for r in journal.rows if r.get('turn') == turn and
                    r.get('kind') == 'tool_result' and r.get('name') == 'task.context']
        if previous:
            if previous[-1].get('error'):
                raise ValueError('Required context read failed')
            return
        alias = next((a for a, t in registry.tools.items() if t['name'] == 'task.context'), None)
        if alias is None:
            return  # Compatibility fixtures/advice may have a narrower catalog.
        call = ToolCallRequest('context-' + str(uuid.uuid5(uuid.NAMESPACE_URL, journal.session + ':' + turn)), alias, {})
        # This is an installer-owned read, not a generated decision. Use the
        # same schema, lease checks and write-ahead journal as every MCP call.
        message = {'role': 'assistant', 'tool_calls': [{'id': call.id, 'type': 'function',
                   'function': {'name': alias, 'arguments': '{}'}}]}
        journal.append({'kind': 'bootstrap', 'turn': turn, 'message': message})
        spec.initial_messages.append(message)
        value, _, _ = await self._run_tool(spec, call)
        spec.initial_messages.append({'role': 'tool', 'tool_call_id': call.id, 'name': alias, 'content': value})
        if registry.last['error']:
            raise ValueError('Required context read failed')

    async def _run_tool(self, spec, call):
        registry = spec.tools
        # A single model response may contain several sequential calls. Once
        # context repair has happened, enforce its per-operation progress cap between calls,
        # before any next write-ahead record or external effect. Before the
        # first repair the existing iteration-boundary behavior is unchanged.
        unlimited_repairs = getattr(getattr(spec,'hook',None),'config',{}).get('max_context_repairs',1) is None
        if (not unlimited_repairs and not getattr(spec,'context_refresh',False) and
                any(r.get('kind')=='recovery' and r.get('turn')==registry.turn for r in registry.journal.rows)):
            check_progress(registry.journal.rows,registry.turn)
        registry.last = {'name': call.name, 'seconds': 0, 'error': True}
        # Transport failures escape AgentRunner's usual catch: an uncertain
        # write must not become a fabricated success or be retried blindly.
        result = await registry.execute_call(call)
        message = {'role': 'tool', 'tool_call_id': call.id, 'name': call.name, 'content': result}
        failed = registry.last['error']
        registry.journal.append({'kind': 'tool_result', 'turn': registry.turn, 'call_id': call.id,
                                 'message': message, **registry.last, 'error': failed, 'terminal': registry.terminal, 'decision': registry.decision, 'waiting_user':registry.waiting_user})
        registry.emit({'type': 'tool', 'name': registry.last['name'], 'error': failed, 'seconds': registry.last['seconds']})
        # Draft creation, rejection and acceptance can all change the scoped
        # tools/list. Refresh only after the result is durable, including
        # between sequential calls from one model response.
        await registry.refresh_after_result(registry.last['name'])
        return result, {'name': call.name, 'status': 'error' if failed else 'ok', 'detail': ''}, None


class DurableHook(AgentHook):
    def __init__(self, journal, turn, provider, tools, config):
        self.journal, self.turn, self.provider, self.tools, self.config = journal, turn, provider, tools, config

    def bind_system_tools(self, context, pending=None):
        """Keep one real-catalog header; durable attachments restore exact bytes."""
        from agent_factory.role_prompts import bind_tools_header
        template=self.config.get('system_prompt','')
        if not context.messages or context.messages[0].get('role')!='system':return
        def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        if pending:
            index=self.journal.rows.index(pending)
            saved=next((r for r in reversed(self.journal.rows[:index]) if r.get('kind')=='system_prompt_bound' and r.get('turn')==self.turn),None)
            if saved:
                if saved['template_sha256']!=digest(template) or saved['message_sha256']!=digest(saved['system_message']):
                    raise ValueError('Pending system prompt binding changed; reconcile the durable request')
                context.messages[0]=dict(saved['system_message'])
            return
        if '\n## Tools\n' not in template:return
        registry=getattr(self.tools,'tools',None)
        if registry is None:return
        text=bind_tools_header(template,[tool['name'] for tool in registry.values()])
        message={'role':'system','content':text}
        previous=next((r for r in reversed(self.journal.rows) if r.get('kind')=='system_prompt_bound' and r.get('turn')==self.turn),None)
        if not previous or previous['system_message']!=message:
            self.journal.append({'kind':'system_prompt_bound','turn':self.turn,'system_message':message,
                'template_sha256':digest(template),'message_sha256':digest(message)})
        context.messages[0]=message

    async def before_iteration(self, context):
        starts = sum(r['kind'] == 'model_start' and r['turn'] == self.turn for r in self.journal.rows)
        output = sum(r.get('usage', {}).get('completion_tokens', 0) for r in self.journal.rows
                     if r['kind'] in ('model', 'model_rejected') and r['turn'] == self.turn)
        pending = getattr(self.provider,'pending_request',lambda:None)()
        limit = self.config.get('max_iterations',64)
        output_limit=self.config.get('max_total_output_tokens',131072)
        if ((limit is not None and (starts>limit or (starts==limit and not pending))) or
                (output_limit is not None and output>=output_limit)):
            raise ValueError('Generation budget exhausted')
        if pending:
            # Finish the admitted generation with its original bytes before
            # checkpoints, headers or context repair can change the payload.
            self.bind_system_tools(context,pending)
            return
        latest=next((r for r in reversed(self.journal.rows) if r.get('turn')==self.turn and r['kind'] in ('model','model_rejected')),None)
        if latest and latest.get('cause') in ('context_window','output_length'):
            checkpoint=next((r for r in reversed(self.journal.rows) if r.get('turn')==self.turn and r['kind']=='recovery'
                             and r.get('reason')==latest['cause'] and r.get('rejected_request_id')==latest.get('request_id')),None)
            if checkpoint is None:
                await self.on_context_rejected(context,ContextWindowExceeded('Restore definitively rejected context'))
            elif not checkpoint.get('context_shrunk'):
                raise ContextWindowExceeded('Saved scoped checkpoint cannot fit retained context')
        try:
            check_progress(self.journal.rows, self.turn)
        except StalledToolProgress:
            from .recovery import repair
            await repair(self, context)
        from .focus import checkpoint
        checkpoint(self.journal,self.turn,context.messages)
        self.bind_system_tools(context)

    async def on_context_rejected(self, context, error):
        from .recovery import recovery_messages, repair
        from .journal import AmbiguousToolOutcome
        if getattr(self.provider,'pending_request',lambda:None)():
            raise AmbiguousToolOutcome('Reconcile durable inference outcome before context rollover')
        def size(messages):
            return len(json.dumps(messages,ensure_ascii=False,allow_nan=False).encode())
        before=size(context.messages)
        rejected=next((r for r in reversed(self.journal.rows) if r.get('turn')==self.turn and r['kind']=='model_rejected'
                       and r.get('cause') in ('context_window','output_length')),{})
        reason=rejected.get('cause','context_window')
        system_message=(dict(context.messages[0]) if context.messages and context.messages[0].get('role')=='system'
                        else {'role':'system','content':self.config['system_prompt']})
        projected=[system_message,
                   *recovery_messages(self.journal.rows,self.turn,further_repairs=True,reason=reason)]
        if size(projected)>=before:
            raise ContextWindowExceeded('Context window cannot fit retained receipts; no safe shrinking checkpoint is available') from error
        await repair(self,context,reason=reason,context_rejection={
            'rejected_request_id':rejected.get('request_id'),'context_before_bytes':before},
            system_message=system_message)
        after=size(context.messages)
        if after>=before:
            raise ContextWindowExceeded('Fresh scoped checkpoint did not shrink context; no repeated generation or receipt truncation') from error
        self.journal.append({'kind':'context_rollover','turn':self.turn,'before_bytes':before,'after_bytes':after,
                             'reason':'output_length' if reason=='output_length' else 'context_length_exceeded',
                             'rejected_request_id':rejected.get('request_id'),'history_retained':True})
        return True

    async def before_execute_tools(self, context):
        self.save_model(context)

    def save_model(self, context):
        message = dict(context.messages[-1])
        message.pop('reasoning_content', None)
        context.messages[-1] = message
        self.journal.append({'kind': 'model', 'turn': self.turn, 'message': message,
                             'usage': context.usage, 'seconds':self.provider.last_metrics['seconds'],
                             **{key:self.provider.last_metrics[key] for key in TIMING_FIELDS if key in self.provider.last_metrics}})

    async def after_iteration(self, context):
        if not context.tool_calls:
            self.save_model(context)
            if self.config.get('structured_finish'):
                raise ValueError('workflow.finish required; prose cannot complete a gate')
            from agent_factory.native_adapter import final_result
            final_result(context.final_content)
        elif self.tools.terminal:
            context.final_content = json.dumps({'waiting_user':True} if self.tools.waiting_user else {'decision':self.tools.decision,'result':{}})
        else:
            return
        self.journal.append({'kind': 'final', 'turn': self.turn, 'text': context.final_content})

    def should_stop(self, context):
        return self.tools.terminal


async def run(config, prompt, session=None):
    journal = Journal(config['session_root'], config['owner'], session,max_bytes=config.get('journal_max_bytes',32*1024*1024))
    mcp = None
    try:
        messages = journal.prepare(config['turn_id'], prompt)
        emit({'type': 'session', 'sessionId': journal.session, 'executor': 'pikobot'})
        previous = journal.final(config['turn_id'])
        if previous:
            emit({'type': 'final', 'text': previous['text']})
            emit({'type': 'status', 'phase': 'turn_end', 'reason': {'kind': 'completed'}})
            return
        mcp = MCP(config['mcp'])
        registry = FactoryTools(mcp, journal, config['turn_id'], emit)
        provider = FactoryProvider(config, journal, config['turn_id'], emit)
        runner = DurableRunner(provider)
        spec = AgentRunSpec([{'role': 'system', 'content': config['system_prompt']}, *messages], registry,
            config['model'], max_iterations=config.get('max_iterations', 64), concurrent_tools=False,
            hook=DurableHook(journal, config['turn_id'], provider, registry, config))
        # Resume only calls with no write-ahead start. Started-but-unanswered
        # operations were rejected by prepare() for service reconciliation.
        last_model = next((r for r in reversed(journal.rows) if r['kind'] in ('model', 'bootstrap') and r['turn'] == config['turn_id']), None)
        if last_model:
            completed = {r['call_id'] for r in journal.rows if r['kind'] == 'tool_result' and r['turn'] == config['turn_id']}
            registry.terminal = any(r.get('terminal') for r in journal.rows if r['kind'] == 'tool_result' and r['turn'] == config['turn_id'])
            for raw in last_model['message'].get('tool_calls', []):
                if raw['id'] in completed: continue
                call = ToolCallRequest(raw['id'], raw['function']['name'], loads(raw['function']['arguments']))
                value, _, _ = await runner._run_tool(spec, call)
                spec.initial_messages.append({'role': 'tool', 'tool_call_id': call.id, 'name': call.name, 'content': value})
            if registry.terminal:
                decision=registry.decision or next(r['decision'] for r in reversed(journal.rows) if r.get('terminal'))
                waiting=any(r.get('waiting_user') and r.get('terminal') for r in journal.rows if r.get('turn')==config['turn_id'])
                text = json.dumps({'waiting_user':True} if waiting else {'decision':decision,'result':{}})
                journal.append({'kind': 'final', 'turn': config['turn_id'], 'text': text})
                emit({'type': 'final', 'text': text})
                emit({'type': 'status', 'phase': 'turn_end', 'reason': {'kind': 'completed'}})
                return
        await runner.prepare_context(spec)
        result = await runner.run(spec)
        if result.stop_reason != 'completed' or not journal.final(config['turn_id']):
            raise RuntimeError('Agent did not complete within its budget')
        emit({'type': 'final', 'text': result.final_content})
        emit({'type': 'status', 'phase': 'turn_end', 'reason': {'kind': 'completed'}})
    finally:
        if mcp: mcp.close()
        journal.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--session-id')
    parser.add_argument('prompt')
    args = parser.parse_args()
    try:
        with open(args.config) as stream: config = loads(stream.read())
        asyncio.run(run(config, args.prompt, args.session_id))
    except Exception as exc:
        emit({'type': 'error', 'error': type(exc).__name__})
        import traceback
        print(json.dumps({'error':type(exc).__name__,'frames':[{'file':__import__('pathlib').Path(f.filename).name,'line':f.lineno,'function':f.name} for f in traceback.extract_tb(exc.__traceback__)]}),file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__': main()
