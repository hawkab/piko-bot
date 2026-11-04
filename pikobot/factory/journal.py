"""Append-only fsynced checkpoints. An uncertain side effect is never replayed."""
import fcntl
import json
import os
from pathlib import Path
import time
import uuid


def loads(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate JSON key')
            result[key] = value
        return result
    def constant(_):
        raise ValueError('Nonfinite JSON')
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


class AmbiguousToolOutcome(RuntimeError):
    """A broker receipt must be reconciled before this session can continue."""


class Journal:
    def __init__(self, root, owner, session=None, *, max_bytes=32*1024*1024):
        if max_bytes is not None and (type(max_bytes) is not int or max_bytes<1):
            raise ValueError("Journal max_bytes must be positive or null")
        self.max_bytes=max_bytes
        self.session = str(uuid.UUID(session)) if session else str(uuid.uuid4())
        root = Path(root); root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = root / (self.session + '.jsonl')
        if session and not self.path.is_file():
            raise ValueError('Checkpoint does not exist in this owner directory')
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.fchmod(self.fd, 0o600)
            if self.max_bytes is not None and os.fstat(self.fd).st_size > self.max_bytes:
                raise ValueError('Checkpoint exceeds budget')
            # Stream the append-only file instead of a fixed 32MiB read. Keep
            # finite profiles keep their record guard; explicit null retains
            # complete checkpoints of any size instead of silently truncating.
            self.rows=[];end=0
            with os.fdopen(os.dup(self.fd),'rb') as stream:
                while True:
                    line=stream.readline() if self.max_bytes is None else stream.readline(2*1024*1024+1)
                    if not line:break
                    if self.max_bytes is not None and len(line)>2*1024*1024:raise ValueError('Checkpoint record exceeds budget')
                    if not line.endswith(b'\n'):
                        os.ftruncate(self.fd,end);os.fsync(self.fd);break
                    self.rows.append(loads(line));end+=len(line)
            os.lseek(self.fd, 0, os.SEEK_END)
            if self.rows:
                if self.rows[0] != {'kind': 'header', 'version': 1, 'owner': owner, 'session': self.session}:
                    raise ValueError('Checkpoint owner/version mismatch')
            else:
                if session:
                    raise ValueError('Checkpoint header missing')
                self.append({'kind': 'header', 'version': 1, 'owner': owner, 'session': self.session}, stamp=False)
                parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
                try: os.fsync(parent)
                finally: os.close(parent)
        except BaseException:
            os.close(self.fd)
            raise

    def preflight(self, rows):
        """Prepare exact timestamped records before a multi-record checkpoint intent."""
        prepared=[];total=os.fstat(self.fd).st_size
        for row in rows:
            row=dict(row,timestamp_ms=time.time_ns()//1000000)
            raw=(json.dumps(row,ensure_ascii=False,allow_nan=False)+'\n').encode()
            total+=len(raw)
            if self.max_bytes is not None and (len(raw)>2*1024*1024 or total>self.max_bytes):
                raise ValueError('Checkpoint budget exhausted')
            prepared.append(row)
        return prepared

    def append(self, row, *, stamp=True):
        row = dict(row, timestamp_ms=time.time_ns() // 1000000) if stamp else dict(row)
        raw = (json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n').encode()
        if self.max_bytes is not None and (len(raw)>2*1024*1024 or os.fstat(self.fd).st_size+len(raw)>self.max_bytes):
            raise ValueError('Checkpoint budget exhausted')
        view = memoryview(raw)
        while view:
            view = view[os.write(self.fd, view):]
        os.fsync(self.fd)
        self.rows.append(row)

    def prepare(self, turn, prompt):
        starts = {(r['turn'], r['call_id']) for r in self.rows if r['kind'] == 'tool_start'}
        done = {(r['turn'], r['call_id']) for r in self.rows if r['kind'] == 'tool_result'}
        if starts - done:
            raise AmbiguousToolOutcome('Uncertain tool result; reconcile service receipts before retry')
        recovering = {(r['turn'],r.get('attempt',1)) for r in self.rows if r['kind'] == 'recovery_started'}
        recovered = {(r['turn'],r.get('attempt',1)) for r in self.rows if r['kind'] == 'recovery'}
        if recovering - recovered:
            raise AmbiguousToolOutcome('Interrupted context repair; inspect receipts before continuation')
        turns = [r for r in self.rows if r['kind'] == 'user']
        if turns and turns[-1]['turn'] == turn:
            if turns[-1]['message']['content'] != prompt:
                raise ValueError('Resume prompt changed')
        else:
            if turns and not any(r['kind'] == 'final' and r['turn'] == turns[-1]['turn'] for r in self.rows):
                raise ValueError('Prior turn incomplete; explicit recovery required')
            self.append({'kind': 'user', 'turn': turn, 'message': {'role': 'user', 'content': prompt}})
        messages = []
        for row in self.rows:
            if row['kind'] == 'recovery': messages = list(row['messages'])
            elif 'message' in row: messages.append(row['message'])
        return messages

    def final(self, turn):
        return next((r for r in reversed(self.rows) if r['kind'] == 'final' and r['turn'] == turn), None)

    def close(self):
        os.close(self.fd)
