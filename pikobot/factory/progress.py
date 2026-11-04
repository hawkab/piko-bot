"""Bound unsuccessful corrections using durable, service-reported validation."""
import hashlib
import json


class StalledToolProgress(RuntimeError):
    pass


def feedback(result):
    values = [result.get('structuredContent')]
    for block in result.get('content', []):
        if block.get('type') == 'text':
            try: values.append(json.loads(block['text']))
            except (ValueError, TypeError): pass
    for value in values:
        if not isinstance(value, dict) or not value.get('error'): continue
        validation = value.get('validation', {})
        count = validation.get('remaining_violations') if isinstance(validation, dict) else None
        if type(count) is int and count > 0:
            return {'category': str(value['error']) + ':coverage', 'remaining': count}
        # Exclude new artifact IDs and other transport metadata: saving the
        # same invalid draft again is not progress. Store no sensitive detail.
        detail = str(value.get('detail', value['error']))
        return {'category': str(value['error']) + ':' + hashlib.sha256(detail.encode()).hexdigest(), 'remaining': 1}
    return None


def check(rows, turn, limit=4):
    states = {}
    for row in rows:
        if row.get('turn') == turn and row.get('kind') == 'recovery':
            states = {}
        if row.get('turn') != turn or row.get('kind') != 'tool_result': continue
        raw_name = row.get('name')
        name = ('autotest_suite' if raw_name in ('autotests.submit','autotests.revise') else
                'workflow_completion' if raw_name in ('workflow.finish','review.submit') else raw_name)
        if not row.get('error'):
            if row.get('validation_reset',True):states.pop(name, None)
            continue
        progress = row.get('validation_progress')
        if not progress: continue
        state=states.setdefault(name,{'best':{},'attempts':0})
        category=progress['category'];previous=state['best'].get(category)
        # A different error is not evidence of progress. Preserve each
        # category's best result across oscillations and read/upload calls.
        if previous is not None and progress['remaining']<previous:state['attempts']=0
        state['best'][category]=min(previous,progress['remaining']) if previous is not None else progress['remaining']
        state['attempts']+=1
    for name, state in states.items():
        if state['attempts'] >= limit:
            raise StalledToolProgress('No validation progress after bounded corrections: ' + name)
