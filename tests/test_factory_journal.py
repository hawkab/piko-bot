"""Portable regression tests for the durable Agent Factory journal."""

__author__ = "Olshansky Grigory"

import json
from pathlib import Path
import tempfile
import unittest

from pikobot.factory.journal import Journal, AmbiguousToolOutcome, loads


class JournalTests(unittest.TestCase):

    def test_resume_preserves_completed_tools_and_refuses_uncertain_effect(self):
        with tempfile.TemporaryDirectory() as root:
            j = Journal(root, 'owner')
            sid = j.session
            j.prepare('job', 'task')
            j.append({'kind': 'tool_start', 'turn': 'job', 'call_id': 'write1', 'name': 'candidate.publish'})
            j.close()
            j = Journal(root, 'owner', sid)
            with self.assertRaises(AmbiguousToolOutcome):
                j.prepare('job', 'task')
            j.append({'kind': 'tool_result', 'turn': 'job', 'call_id': 'write1', 'message': {'role': 'tool', 'content': 'actual receipt'}})
            j.append({'kind': 'final', 'turn': 'job', 'text': 'done'})
            j.close()
            j = Journal(root, 'owner', sid)
            self.assertIn('actual receipt', json.dumps(j.prepare('next', 'new gate')))
            self.assertEqual(j.path.stat().st_mode & 511, 384)
            j.close()

    def test_owner_lock_and_truncated_last_append(self):
        with tempfile.TemporaryDirectory() as root:
            j = Journal(root, 'owner')
            sid = j.session
            j.prepare('job', 'task')
            with self.assertRaises(BlockingIOError):
                Journal(root, 'owner', sid)
            j.close()
            with self.assertRaises(ValueError):
                Journal(root, 'other owner', sid)
            with open(Path(root) / (sid + '.jsonl'), 'ab') as f:
                f.write(b'{"partial":')
            j = Journal(root, 'owner', sid)
            self.assertEqual(j.prepare('job', 'task')[-1]['content'], 'task')
            with self.assertRaises(ValueError):
                j.prepare('job', 'changed task')
            with self.assertRaises(ValueError):
                j.prepare('new job', 'new task')
            j.close()

    def test_json_never_repairs_or_coerces(self):
        for value in ('{"a":1,"a":2}', '{"a":NaN}', '{"a":1,}', "{'a':1}"):
            with self.assertRaises(ValueError):
                loads(value)

    def test_checkpoint_paths_cannot_escape_owner(self):
        with tempfile.TemporaryDirectory() as root:
            for value in ('../../other', 'session-old-dsh', '123'):
                with self.assertRaises(ValueError):
                    Journal(root, 'owner', value)
