"""Strict OpenAI-compatible transport to the per-lease loopback gateway only."""
import asyncio
import hashlib
import json
import itertools
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from pikobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest, ContextWindowExceeded
from .journal import loads


TIMING_FIELDS=('seconds','last_attempt_seconds','last_attempt_clock','logical_started_at_ms','seconds_source','seconds_clock',
               'seconds_scope','logical_timing_complete','logical_timing_fallback')


def elapsed_metrics(pending,attempt_started):
    """Request elapsed wall time, never GPU/decode time or a budget clock."""
    attempt=round(max(0.0,time.monotonic()-attempt_started),3)
    field='logical_started_at_ms' if 'logical_started_at_ms' in pending else 'timestamp_ms'
    first=pending.get(field);now=time.time_ns()//1000000
    reason=None
    if type(first) is not int or first<=0:
        reason='missing_start' if first is None else 'invalid_start'
    elif now<first:reason='wall_clock_before_start'
    elif (now-first)/1000+0.002<attempt:reason='wall_clock_behind_last_attempt'
    if reason:
        return dict(seconds=attempt,last_attempt_seconds=attempt,last_attempt_clock='monotonic',
            logical_started_at_ms=first if type(first) is int and first>0 else None,
            seconds_source='last_attempt_monotonic',seconds_clock='monotonic',seconds_scope='last_attempt_only',
            logical_timing_complete=False,logical_timing_fallback=reason)
    return dict(seconds=round((now-first)/1000,3),last_attempt_seconds=attempt,last_attempt_clock='monotonic',logical_started_at_ms=first,
        seconds_source='inference_request.'+field,seconds_clock='unix_wall_milliseconds',
        seconds_scope='logical_request_wall_including_waits',logical_timing_complete=True,logical_timing_fallback=None)


class FactoryProvider(LLMProvider):
    def get_default_model(self):
        return self.config['model']

    def __init__(self, config, journal, turn, emit):
        super().__init__()
        url = urllib.parse.urlsplit(config['model_url'])
        if url.scheme != 'http' or url.hostname != '127.0.0.1' or url.username or url.password or url.query or url.fragment:
            raise ValueError('Factory inference must use the local lease gateway')
        self.config, self.journal, self.turn, self.emit = config, journal, turn, emit
        self.returned_request_id = None
        self.url = config['model_url'].rstrip('/') + '/chat/completions'
        self.key = os.environ['AF_INFERENCE_KEY']
        # No environment proxy or redirect can carry a capability elsewhere.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs): return None
        self.http = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def pending_request(self):
        consumed = sum(row['kind'] in ('model', 'model_rejected') and row.get('turn') == self.turn
                       for row in self.journal.rows)
        return next((row for row in reversed(self.journal.rows)
                     if row['kind'] == 'inference_request' and row.get('turn') == self.turn
                     and row['consumed_models'] == consumed and row['request_id'] != self.returned_request_id), None)

    async def chat_with_retry(self, **kwargs):
        # Gateway unavailable responses may reattach the same durable Spark
        # generation. A fresh UUID is never allocated for an HTTP retry.
        # Broken/partial local responses remain explicit, not guessed output.
        limit=self.config.get('inference_retry_attempts',None if self.config.get('max_iterations',64) is None else 3)
        if limit is not None and (type(limit) is not int or limit<1):
            raise ValueError('inference_retry_attempts must be positive or null')
        for attempt in itertools.count():
            try: return await self.chat(**kwargs)
            except urllib.error.HTTPError as exc:
                status=exc.code;exc.close()
                if status not in (429,503) or (limit is not None and attempt+1>=limit):raise
                self.journal.append({'kind':'model_unavailable','turn':self.turn,'status':status})
                await asyncio.sleep(self.config.get('inference_cooldown_seconds',30)+1)

    async def chat(self, messages, tools=None, model=None, **kwargs):
        # Modern gates can only complete through a controller-validated tool.
        # auto also admits empty/prose-only final answers, which contradicts
        # that protocol. required still allows reads and an explicit rejection;
        # it does not select an approval or bypass service evidence checks.
        structured = bool(self.config.get('structured_finish'))
        if structured and not tools:
            raise ValueError('Structured gate has no tools')
        payload = {'model': model, 'messages': messages, 'tools': tools or [], 'stream': False,
                   'tool_choice': 'required' if structured else 'auto',
                   'parallel_tool_calls': False}
        if self.config.get('max_output_tokens') is not None:
            payload['max_tokens']=self.config['max_output_tokens']
        if not tools:
            payload.pop('tools'); payload.pop('tool_choice'); payload.pop('parallel_tool_calls')
        raw = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
        context_limit=self.config.get('max_context_bytes',2*1024*1024)
        if context_limit is not None and len(raw)>context_limit:
            raise ValueError('Context budget exhausted; no silent truncation')
        payload_sha256 = hashlib.sha256(raw).hexdigest()
        consumed = sum(row['kind'] in ('model', 'model_rejected') and row.get('turn') == self.turn
                       for row in self.journal.rows)
        pending = self.pending_request()
        if pending:
            if pending['payload_sha256'] != payload_sha256:
                raise ValueError('Unconsumed inference request changed; reconcile its durable result first')
            request_id = str(uuid.UUID(pending['request_id']))
        else:
            request_id = str(uuid.uuid4())
            self.journal.append({'kind': 'inference_request', 'turn': self.turn, 'request_id': request_id,
                                 'payload_sha256': payload_sha256, 'consumed_models': consumed,
                                 'logical_started_at_ms': time.time_ns()//1000000})
            pending = self.journal.rows[-1]
            self.journal.append({'kind': 'model_start', 'turn': self.turn})
        started = time.monotonic()
        self.journal.append({'kind': 'model_request_attempt', 'turn': self.turn, 'request_id': request_id})
        def request():
            req = urllib.request.Request(self.url, raw, {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + self.key,
                                                        'X-AF-Inference-Request-ID': request_id})
            with self.http.open(req, timeout=self.config.get('inference_timeout_ms', 225000) / 1000) as response:
                body = response.read(2 * 1024 * 1024 + 1)
            if len(body) > 2 * 1024 * 1024:
                raise ValueError('Model response exceeds budget')
            return loads(body)
        try:
            result = await asyncio.to_thread(request)
        except urllib.error.HTTPError as exc:
            # Only the trusted loopback gateway's explicit typed rejection
            # proves this durable request has a final outcome without output.
            # Other 400s, partial bodies and network failures stay unresolved.
            if exc.code==400:
                try:
                    body=exc.read(8193)
                    value=loads(body) if len(body)<=8192 else None
                except (ValueError,OSError):value=None
                if isinstance(value,dict) and isinstance(value.get('error'),dict) and value['error'].get('code')=='context_length_exceeded':
                    exc.close()
                    self.journal.append({'kind':'model_rejected','turn':self.turn,'request_id':request_id,
                                         'usage':{},'cause':'context_window','finish_reason':'context_length_exceeded'})
                    raise ContextWindowExceeded('Provider context window exceeded; no partial commands executed') from exc
            raise
        usage = {k: int(result.get('usage', {}).get(k, 0) or 0) for k in ('prompt_tokens', 'completion_tokens')}
        metrics = dict(usage, **elapsed_metrics(pending,started))
        self.emit({'type': 'usage', **metrics})
        self.last_metrics = metrics
        choices = result.get('choices', [])
        if len(choices) != 1 or choices[0].get('finish_reason') not in ('stop', 'tool_calls'):
            reason = choices[0].get('finish_reason') if len(choices) == 1 else None
            # A complete length response has a known final outcome. With no
            # caller output cap, let the executor checkpoint the existing work
            # before another generation. Never parse/execute partial commands.
            rollover=reason=='length' and self.config.get('max_output_tokens') is None
            self.journal.append({'kind': 'model_rejected', 'turn': self.turn, 'request_id':request_id,'usage': usage,
                                 **{key:metrics[key] for key in TIMING_FIELDS}, 'cause': 'output_length' if rollover else 'incomplete_output',
                                 'finish_reason': reason if reason in ('length', 'content_filter', 'stop', 'tool_calls') else 'unknown'})
            if rollover:
                raise ContextWindowExceeded('Provider ended an incomplete generation at its output length; no partial commands executed')
            raise ValueError('Incomplete model output')
        message = choices[0]['message']
        calls = []
        for item in message.get('tool_calls') or []:
            function = item['function']
            args = loads(function['arguments'])
            if not isinstance(args, dict):
                raise ValueError('Tool arguments must be a JSON object')
            calls.append(ToolCallRequest(item['id'], function['name'], args))
        prior_ids={c['id'] for row in self.journal.rows if row['kind'] in ('model','bootstrap') and row['turn']==self.turn for c in row['message'].get('tool_calls',[])}
        if any(not isinstance(c.id,str) or not 1<=len(c.id)<=128 or c.id in prior_ids or not isinstance(c.name,str) for c in calls):
            raise ValueError('Invalid or reused tool call identity')
        if len(calls) > 16 or len({c.id for c in calls}) != len(calls):
            raise ValueError('Invalid tool batch')
        self.returned_request_id = request_id
        # The hook persists the assistant message together with these metrics
        # before any tool executes.
        return LLMResponse(message.get('content'), calls, choices[0]['finish_reason'], usage,
                           reasoning_content=message.get('reasoning_content'))
