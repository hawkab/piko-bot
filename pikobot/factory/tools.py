"""Exact MCP schemas, explicit errors, no local shell or implicit tool access."""
import asyncio
import hashlib
import json
import os
import queue
import subprocess
import threading
import time

from agent_factory.tool_schema import validate
from .journal import loads
from .progress import feedback


class MCP:
    def __init__(self, config):
        env = {'PATH': os.environ['PATH'], 'LANG': 'C.UTF-8', **config.get('env', {})}
        self.process = subprocess.Popen([config['command'], *config.get('args', [])], env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.queue = queue.Queue(maxsize=32); self.sequence = 0
        self.timeout = config.get('toolCallTimeoutMs', 3600000) / 1000
        def read():
            try:
                while True:
                    line = self.process.stdout.readline(2 * 1024 * 1024 + 1)
                    if not line or len(line) > 2 * 1024 * 1024: break
                    self.queue.put(loads(line), timeout=2)
            except Exception: pass
            finally:
                try: self.queue.put(None, timeout=2)
                except queue.Full: pass
        threading.Thread(target=read, daemon=True).start()
        try:
            self.call('initialize', {'protocolVersion': '2025-11-25', 'capabilities': {},
                                    'clientInfo': {'name': 'pikobot-factory', 'version': '1'}})
            self.send({'jsonrpc': '2.0', 'method': 'notifications/initialized', 'params': {}})
        except BaseException:
            self.close(); raise

    def send(self, row):
        self.process.stdin.write((json.dumps(row, allow_nan=False) + '\n').encode()); self.process.stdin.flush()

    def call(self, method, params):
        self.sequence += 1
        self.send({'jsonrpc': '2.0', 'id': self.sequence, 'method': method, 'params': params})
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            try: row = self.queue.get(timeout=min(1, max(.01, deadline - time.monotonic())))
            except queue.Empty: continue
            if row is None: raise RuntimeError('MCP transport closed; result unknown')
            if row.get('id') == self.sequence:
                if 'error' in row:
                    # A transport/service exception cannot prove whether an
                    # external effect happened. Leave the write-ahead record.
                    raise RuntimeError('MCP RPC failed; reconcile service outcome')
                return row['result']
        raise TimeoutError('MCP deadline; result unknown')

    def close(self):
        self.process.terminate()
        try: self.process.wait(timeout=5)
        except subprocess.TimeoutExpired: self.process.kill(); self.process.wait(timeout=5)
        for stream in (self.process.stdin, self.process.stdout): stream.close()


class FactoryTools:
    # These operations can change the server-issued artifact IDs or expose a
    # draft even when validation returns isError. The scoped server remains
    # the authority for both tool availability and argument schemas.
    CATALOG_CHANGES = frozenset(('task.context', 'analysis.submit', 'autotests.source', 'autotests.reuse',
                                'autotests.patch', 'autotests.submit', 'autotests.revise'))

    def __init__(self, mcp, journal, turn, emit):
        self.mcp, self.journal, self.turn, self.emit = mcp, journal, turn, emit
        self.catalog_ready = False
        self.tools = self.read_catalog()
        self.requires_finish = any(t['name'] == 'workflow.finish' for t in self.tools.values())
        self.catalog_ready = True
        self.terminal = False
        self.waiting_user = False
        self.decision = None
        self.suite_frozen = any(r.get('suite_frozen') and r.get('turn')==turn and r.get('kind')=='tool_result' for r in journal.rows)

    def read_catalog(self):
        catalog = self.mcp.call('tools/list', {})
        if not isinstance(catalog, dict) or catalog.get('isError') or catalog.get('nextCursor') or not isinstance(catalog.get('tools'), list):
            raise ValueError('Incomplete scoped MCP catalog')
        tools = {}
        for tool in catalog['tools']:
            if not isinstance(tool, dict) or not isinstance(tool.get('name'), str) or not tool['name'] or not isinstance(tool.get('inputSchema'), dict):
                raise ValueError('Invalid scoped MCP tool')
            raw = tool['name']
            alias = 'af_' + ''.join(c if c.isascii() and c.isalnum() else '_' for c in raw)[:44] + '_' + hashlib.sha256(raw.encode()).hexdigest()[:12]
            if alias in tools: raise ValueError('Duplicate MCP name')
            tools[alias] = tool
        return tools

    async def refresh_after_result(self, name):
        if name not in self.CATALOG_CHANGES or self.terminal:
            return
        # The caller must durably record the actual tool result first. A
        # catalog read failure must never make its preceding write ambiguous
        # or permit another call against stale schemas.
        self.catalog_ready = False
        tools = await asyncio.to_thread(self.read_catalog)
        if self.requires_finish and not any(t['name'] == 'workflow.finish' for t in tools.values()):
            raise ValueError('Scoped MCP catalog lost workflow.finish')
        self.tools = tools
        self.catalog_ready = True

    def available(self, tool):
        return not (self.suite_frozen and tool['name'] in ('autotests.source','autotests.submit','autotests.revise','autotests.reuse','autotests.patch'))

    def get_definitions(self):
        if not self.catalog_ready:
            raise ValueError('Scoped MCP catalog refresh required')
        return [{'type': 'function', 'function': {'name': alias, 'description': t['name'] + ': ' + t.get('description', ''),
                 'parameters': t['inputSchema'], 'strict': True}} for alias, t in self.tools.items() if self.available(t)]

    async def execute_call(self, call):
        if not self.catalog_ready:
            raise ValueError('Scoped MCP catalog refresh required')
        tool = self.tools.get(call.name)
        if tool is None:
            self.last = {'name': call.name, 'seconds': 0, 'error': True,
                         'validation_progress': {'category': 'tool_not_available', 'remaining': 1}}
            return self.error('Unknown tool; use this gate catalog only')
        self.last={'name':tool['name'],'seconds':0,'error':True}
        try: validate(call.arguments, tool['inputSchema'])
        except ValueError as exc:
            self.last['validation_progress']={'category':'tool_arguments_invalid','remaining':1}
            return self.error(str(exc))
        if self.terminal: return self.error('Gate already completed; no further calls allowed')
        if not self.available(tool):return self.error('Suite is already submitted and immutable. Read task.context if needed, then make your explicit workflow.finish decision. This call made no changes.')
        # Write-ahead record before external effect. Transport death leaves it
        # unresolved so recovery cannot blindly issue the operation a second time.
        self.journal.append({'kind': 'tool_start', 'turn': self.turn, 'call_id': call.id, 'name': tool['name']})
        started = time.monotonic()
        result = await asyncio.to_thread(self.mcp.call, 'tools/call', {'name': tool['name'], 'arguments': call.arguments})
        # Keep MCP text/structured content and isError. No argument repair,
        # str(dict), data loss, or credentials in event stdout.
        content = []
        for block in result.get('content', []):
            if block.get('type') == 'text': content.append(block['text'])
            else: content.append('[Non-text MCP content retained in checkpoint; use browser DOM/OCR or the configured image tools.]')
        text = '\n'.join(content)
        if not text and 'structuredContent' in result:text=json.dumps(result['structuredContent'],ensure_ascii=False)
        output = self.error(text) if result.get('isError') else text
        recorded = False
        for block in result.get('content', []):
            if block.get('type') == 'text':
                try: recorded = recorded or loads(block['text']).get('recorded') is True
                except (ValueError, AttributeError): pass
        if isinstance(result.get('structuredContent'), dict): recorded |= result['structuredContent'].get('recorded') is True
        self.waiting_user = tool['name']=='user.wait' and not result.get('isError') and recorded
        self.terminal = self.waiting_user or (tool['name'] in ('workflow.finish','review.submit') and not result.get('isError') and recorded)
        if self.terminal and not self.waiting_user:self.decision=call.arguments['decision']
        self.last = {'name': tool['name'], 'seconds': round(time.monotonic() - started, 3),
                     'error': bool(result.get('isError'))}
        progress = feedback(result) if result.get('isError') else None
        if progress: self.last['validation_progress'] = progress
        if tool['name'] in ('autotests.submit','autotests.revise'):
            # Uploading a source through the legacy overloaded submit API is
            # not an accepted suite and must not reset its repair budget.
            values=[result.get('structuredContent')]
            for block in result.get('content',[]):
                if block.get('type')=='text':
                    try:values.append(loads(block['text']))
                    except ValueError:pass
            self.last['validation_reset']=not result.get('isError') and any(isinstance(v,dict) and v.get('accepted') is True for v in values)
            self.suite_frozen |= any(isinstance(v,dict) and v.get('suite_frozen') is True for v in values)
            self.last['suite_frozen']=self.suite_frozen
        nontext=[b for b in result.get('content',[]) if b.get('type')!='text']
        if nontext:self.last['nontext_content']=nontext
        return output

    @staticmethod
    def error(message):
        return json.dumps({'isError': True, 'content': message})
