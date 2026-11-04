"""Durable context checkpoints, never a replay of an uncertain operation."""
import hashlib
import json
import re
import uuid
from types import SimpleNamespace

from pikobot.providers.base import ToolCallRequest
from .journal import AmbiguousToolOutcome,loads
from .progress import StalledToolProgress


def has_saved_evidence(row):
    """An unsuccessful validation may still have saved an immutable draft.

    Inspect only structured identifier fields, never infer effects from prose.
    Preserve the original complete call/result pair rather than merging its
    contents with another attempt or treating a saved draft as an accepted suite.
    """
    identifiers={'artifact_id','draft_artifact_id','source_artifact_id','receipt_id',
                 'suite_artifact_id','test_report_id','evidence_artifact_id'}
    def contains(value,depth=0):
        if depth>12:return False
        if isinstance(value,str):
            try:value=json.loads(value)
            except (ValueError,TypeError):return False
        if isinstance(value,dict):
            return any(key in identifiers and isinstance(item,str) and bool(item) or
                       contains(item,depth+1) for key,item in value.items())
        if isinstance(value,list):return any(contains(item,depth+1) for item in value)
        return False
    return contains(row.get('message',{}).get('content'))



# Only side-effect-free reads may be removed after exact arguments AND returned
# bytes match. Checks, captures, memory retrieval audit and source writes are
# deliberately absent: their completed observations/effects remain visible.
READ_ONLY_DEDUP = frozenset(('instructions.read', 'spec.read', 'artifact.read',
    'autotests.baseline', 'code.read', 'code.inspect', 'code.search', 'code.search_many'))
DRAFT_WRITES = frozenset(('autotests.submit', 'autotests.revise'))
EVIDENCE_IDS = frozenset(('artifact_id', 'draft_artifact_id', 'source_artifact_id',
    'receipt_id', 'suite_artifact_id', 'test_report_id', 'evidence_artifact_id'))


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def _structured(value, depth=0):
    if depth > 12: return
    if isinstance(value, str):
        try: value = json.loads(value)
        except (ValueError, TypeError): return
    if isinstance(value, dict):
        yield value
        for item in value.values(): yield from _structured(item, depth+1)
    elif isinstance(value, list):
        for item in value: yield from _structured(item, depth+1)


def _scope(row, turn):
    for value in _structured(row.get('message', {}).get('content')):
        run, job = value.get('run'), value.get('job')
        if not isinstance(run, dict) or not isinstance(job, dict): continue
        binding = (run.get('id'), run.get('project_id'), run.get('task_id'), run.get('task_revision'), run.get('epoch'),
                   job.get('id'), job.get('current_epoch'), job.get('role'), job.get('purpose'), job.get('stage'))
        if any(item is None or item == '' for item in binding): continue
        if ':' in turn and turn.rsplit(':', 1)[1].isdigit() and turn != str(job['id'])+':'+str(job['current_epoch']):
            raise AmbiguousToolOutcome('Scoped context does not belong to this execution')
        return binding
    return None


def _rejected_draft(row):
    return row.get('error') and row.get('name') in DRAFT_WRITES and any(
        value.get('accepted') is False and isinstance(value.get('draft_artifact_id'), str)
        and bool(value['draft_artifact_id']) for value in _structured(row.get('message', {}).get('content')))


def _draft_reference(call,row,binding):
    """Recognize only the new server-attested immutable source contract."""
    try:
        value=loads(row['message']['content']);args=loads(call['function']['arguments'])
        ref=value['source_reference']
    except (ValueError,TypeError,KeyError):return None
    fields={'schema_version','artifact_id','path','digest','source_sha256','source_bytes',
            'run_id','run_epoch','job_id','job_epoch','role','draft_only','suite_submitted'}
    if (not isinstance(ref,dict) or set(ref)!=fields or type(ref.get('schema_version')) is not int or ref['schema_version']!=1
            or ref.get('draft_only') is not True or ref.get('suite_submitted') is not False
            or value.get('suite_submitted') is not False or ref.get('role')!='tester'
            or (ref.get('run_id'),ref.get('run_epoch'),ref.get('job_id'),ref.get('job_epoch'),ref.get('role'))
               !=(binding[0],binding[4],binding[5],binding[6],binding[7])
            or type(ref.get('run_epoch')) is not int or type(ref.get('job_epoch')) is not int
            or ref['run_epoch']<1 or ref['job_epoch']<1
            or type(ref.get('source_bytes')) is not int or not 0<=ref['source_bytes']<=64000
            or any(not isinstance(ref.get(k),str) or not re.fullmatch(r'[a-f0-9]{64}',ref[k]) for k in ('digest','source_sha256'))
            or ref.get('path')!=value.get('path') or ref.get('artifact_id')!=value.get('source_artifact_id')
            or not isinstance(ref.get('path'),str) or not re.fullmatch(r'(api|webdriver|source|infra)/test_[a-z0-9_]+\.py',ref['path'])):
        return None
    expected=str(uuid.uuid5(uuid.NAMESPACE_URL,str(ref['run_id'])+':note:'+str(ref['job_id'])+':'+ref['digest']))
    if ref['artifact_id']!=expected:return None
    if row['name']=='autotests.source':
        if not isinstance(args,dict) or set(args)!={'file_path','source'} or args['file_path']!=ref['path'] or not isinstance(args['source'],str):return None
        raw=args['source'].encode()
        if (len(raw)!=ref['source_bytes'] or hashlib.sha256(raw).hexdigest()!=ref['source_sha256']
                or hashlib.sha256(_canonical({'test_source':1,'path':ref['path'],'content':args['source']}).encode()).hexdigest()!=ref['digest']):
            return None
    elif (not isinstance(args,dict) or set(args)-{'source_artifact_id','old','new','path'}
            or any(not isinstance(args.get(k),str) for k in ('source_artifact_id','old','new'))
            or not args['old'] or ('path' in args and args['path']!=ref['path'])):
        return None
    return ref


def _externalized_sources(results,calls,bound,turn):
    if not bound:return {}
    binding=bound[-1][1]
    if binding[7:9]!=('tester','test_authoring') or turn!=str(binding[5])+':'+str(binding[6]):return {}
    # Once accepted/frozen, source variants may be acceptance evidence. This
    # optimization is exclusively for the current independent draft author.
    if any(not row.get('error') and row.get('name') in ('autotests.submit','autotests.revise','autotests.publish','autotests.run')
           for row in results.values()):return {}
    for row in results.values():
        if row.get('name')!='task.context' or row.get('error'):continue
        for value in _structured(row.get('message',{}).get('content')):
            run=value.get('run')
            if isinstance(run,dict) and run.get('id')==binding[0] and (run.get('autotest_commit') or run.get('suite_frozen') is True):return {}
    candidates={};latest={};full={}
    for call_id,row in results.items():
        if row.get('error') or row.get('name') not in ('autotests.source','autotests.patch'):continue
        ref=_draft_reference(calls[call_id],row,binding)
        if ref is None:continue
        candidates[call_id]=ref;latest[ref['path']]=call_id
        if row['name']=='autotests.source':full[ref['path']]=call_id
    keep=set(latest.values())|set(full.values())
    return {call_id:ref for call_id,ref in candidates.items() if call_id not in keep}


def _projection_selection(records, results, turn):
    calls = {call['id']: call for row in records if row['kind'] in ('bootstrap', 'model')
             for call in row['message'].get('tool_calls', [])}
    if set(results) - set(calls):
        raise AmbiguousToolOutcome('Tool result has no original recorded call')
    pairs = list(results.items())
    for call_id, row in pairs:
        if row.get('message', {}).get('tool_call_id') != call_id:
            raise AmbiguousToolOutcome('Tool result does not match its original call')
    omitted = {}
    contexts = [(call_id, _scope(row, turn)) for call_id, row in pairs
                if row.get('name') == 'task.context' and not row.get('error')]
    bound = [(call_id, scope) for call_id, scope in contexts if scope is not None]
    if bound:
        if any(scope != bound[-1][1] for _, scope in bound):
            raise AmbiguousToolOutcome('Cannot compact different scoped task contexts')
        for call_id, _ in bound[:-1]: omitted[call_id] = 'superseded_scoped_context'
    # Prefer the last exact observation; do not collapse a changed reply or
    # mutable path read merely because its pathname is the same.
    seen = set()
    for call_id, row in reversed(pairs):
        if row.get('error') or row.get('name') not in READ_ONLY_DEDUP: continue
        message = dict(row['message']); message.pop('tool_call_id', None)
        key = (row['name'], calls[call_id]['function']['arguments'], _canonical(message))
        if key in seen: omitted[call_id] = 'identical_read'
        else: seen.add(key)
    drafts = [call_id for call_id, row in pairs if _rejected_draft(row)]
    errors = [call_id for call_id, row in pairs if row.get('error')]
    for call_id in drafts[:-1]:
        if not errors or call_id != errors[-1]: omitted[call_id] = 'superseded_rejected_draft'
    externalized=_externalized_sources(results,calls,bound,turn)
    for call_id in externalized:omitted[call_id]='externalized_owned_draft'
    inventory = []
    for call_id, row in pairs:
        if call_id not in omitted: continue
        content = row['message'].get('content')
        content_bytes = (content if isinstance(content, str) else _canonical(content)).encode()
        ids = sorted({item for value in _structured(content) for key, item in value.items()
                      if key in EVIDENCE_IDS and isinstance(item, str) and item})
        item={'call_id': call_id, 'name': row.get('name'), 'reason': omitted[call_id],
            'arguments_sha256': hashlib.sha256(calls[call_id]['function']['arguments'].encode()).hexdigest(),
            'result_content_sha256': hashlib.sha256(content_bytes).hexdigest(), 'artifact_ids': ids,
            'error': bool(row.get('error')), 'journal_reference': {'turn': turn, 'call_id': call_id}}
        if call_id in externalized:item['source_reference']=externalized[call_id]
        inventory.append(item)
    return omitted, inventory


def recovery_messages(rows, turn, *, further_repairs=False, reason="validation_stalled"):
    """Keep exact effects and current evidence; project superseded diagnostic history.

    Do not summarize artifacts with another model: preserve the actual receipts,
    including source IDs needed to continue the independent author's work.
    """
    records = [r for r in rows if r.get('turn') == turn]
    results = {r['call_id']: r for r in records if r['kind'] == 'tool_result'}
    if {r['call_id'] for r in records if r['kind'] == 'tool_start'} - set(results):
        raise AmbiguousToolOutcome('Reconcile uncertain effects before context projection')
    omitted, inventory = _projection_selection(records, results, turn)
    failures = [r['call_id'] for r in records if r['kind'] == 'tool_result' and r.get('error')]
    last_error = failures[-1] if failures else None
    messages = [r['message'] for r in records if r['kind'] == 'user']
    for row in records:
        if row['kind'] not in ('bootstrap', 'model'): continue
        calls = row['message'].get('tool_calls', [])
        retained = [c for c in calls if c['id'] in results and c['id'] not in omitted and
                    (not results[c['id']].get('error') or c['id'] == last_error or
                     has_saved_evidence(results[c['id']]))]
        if not retained: continue
        messages.append({'role': 'assistant', 'content': '', 'tool_calls': retained})
        messages.extend(results[c['id']]['message'] for c in retained)
    if inventory:
        messages.append({'role': 'user', 'content': _canonical({
            'deterministic_context_projection': 1, 'omitted_pairs': inventory,
            'history': 'Original complete call/result pairs remain in the append-only execution journal. This inventory is not a tool result, approval or permission to replay an operation.',
            **({'draft_restore':{'tool':'artifact.read','argument':'artifact_id','content':'Immutable test_source/path/content envelope; variants remain available, not declared superseded or approved.'}}
               if any(item['reason']=='externalized_owned_draft' for item in inventory) else {})})})
    messages.append({'role': 'user', 'content':
        ('The provider definitively rejected the context length; the executor made a deterministic scoped checkpoint. '
         if reason=='context_window' else
         'The provider ended an incomplete generation at its output length. No partial command was executed; the executor checkpointed the previously completed work. '
         if reason=='output_length' else 'The executor detected repeated validation failures and refreshed the scoped context at this checkpoint. ') +
        'Keep the completed effects and exact input bindings of the receipts above; do not replay an unchanged '
        'completed operation merely to rebuild context. A completed check can be a failing red observation, '
        'not approval. Changed source or commit requires fresh registered checks; fix the owning candidate, '
        'commit it and run the required checks for that new SHA through the normal scoped tools. '
        'Old receipts cannot certify changed inputs. Publication or deployment of a new candidate still '
        'requires the normal ownership, review and Guardian gates. An unknown outcome must be reconciled, '
        'never treated as permission to retry. '
        'Use the current tool schema and the precise refusal to change the invalid action, not its wording. '
        'If the requirements or available evidence cannot be satisfied, explicitly reject via workflow.finish. '
        'Do not weaken requirements, tests, role permissions or review gates. ' +
        ('Further context checkpoints remain available; original journal lineage and unknown-outcome holds are retained.'
         if further_repairs else 'No further context repair is available.')})
    return messages


async def repair(hook, context, *, reason="validation_stalled", context_rejection=None, system_message=None):
    journal, turn, registry = hook.journal, hook.turn, hook.tools
    rows = [r for r in journal.rows if r.get('turn') == turn]
    attempts=[r.get('attempt',1) for r in rows if r['kind']=='recovery_started']
    done={r.get('attempt',1) for r in rows if r['kind']=='recovery'}
    if set(attempts)-done:
        raise AmbiguousToolOutcome('Interrupted context repair; reconcile receipts before continuation')
    limit=hook.config.get('max_context_repairs',1)
    if limit is not None and (type(limit) is not int or limit<1):
        raise ValueError('max_context_repairs must be positive or null')
    if limit is not None and len(attempts)>=limit:
        raise StalledToolProgress('Context repair exhausted; DevOps incident required')
    attempt=max(attempts,default=0)+1
    starts = {r['call_id'] for r in rows if r['kind'] == 'tool_start'}
    completed = {r['call_id'] for r in rows if r['kind'] == 'tool_result'}
    if starts - completed:
        raise AmbiguousToolOutcome('Reconcile uncertain effects before context repair')
    alias = next((a for a, t in registry.tools.items() if t['name'] == 'task.context'), None)
    if alias is None:
        raise StalledToolProgress('No scoped context tool; DevOps incident required')
    call = ToolCallRequest('recovery-' + str(uuid.uuid5(uuid.NAMESPACE_URL, journal.session + ':' + turn + (':'+str(attempt) if attempt>1 else ''))), alias, {})
    message = {'role': 'assistant', 'tool_calls': [{'id': call.id, 'type': 'function',
               'function': {'name': alias, 'arguments': '{}'}}]}
    journal.append({'kind': 'bootstrap', 'turn': turn, 'message': message})
    from .__main__ import DurableRunner
    await DurableRunner(None)._run_tool(SimpleNamespace(tools=registry,context_refresh=True), call)
    if registry.last['error']:
        raise StalledToolProgress('Scoped context refresh failed; DevOps incident required')
    messages = recovery_messages(journal.rows, turn, further_repairs=limit is None or attempt<limit,reason=reason)
    # Serialize before the durable append so malformed evidence cannot replace
    # a usable checkpoint. Preserve the original total generation/token budget.
    json.dumps(messages, allow_nan=False)
    full_messages=[system_message or {'role':'system','content':hook.config['system_prompt']},*messages]
    metadata={'reason':reason}
    if context_rejection is not None:
        metadata.update(context_rejection)
        metadata['context_after_bytes']=len(json.dumps(full_messages,ensure_ascii=False,allow_nan=False).encode())
        metadata['context_shrunk']=metadata['context_after_bytes']<metadata['context_before_bytes']
    # Complete the harmless scoped READ and serialize/size-check both records
    # before the repair marker. A finite allocation refusal must not fabricate
    # an interrupted repair that could never have produced a checkpoint.
    batch=journal.preflight([{'kind':'recovery_started','turn':turn,'attempt':attempt},
        {'kind':'recovery','turn':turn,'attempt':attempt,'messages':messages,**metadata}])
    for row in batch:journal.append(row,stamp=False)
    context.messages[:] = full_messages
    registry.emit({'type': 'recovery', 'attempt': attempt, 'reason': reason})
