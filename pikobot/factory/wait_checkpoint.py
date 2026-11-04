"""Reconcile only a service-attested user.wait after native process termination."""
import json
from .journal import AmbiguousToolOutcome,loads


def reconcile(journal,turn,receipt):
    bound=receipt['binding']
    if turn!=bound['job_id']+':'+str(bound['job_epoch']):raise PermissionError('Wait turn differs from receipt')
    starts={(r['turn'],r['call_id']):r for r in journal.rows if r['kind']=='tool_start'}
    done={(r['turn'],r['call_id']) for r in journal.rows if r['kind']=='tool_result'}
    missing=set(starts)-done
    if len(missing)>1:raise AmbiguousToolOutcome('Other unknown tool outcomes require reconciliation before user continuation')
    recovering={(r['turn'],r.get('attempt',1)) for r in journal.rows if r['kind']=='recovery_started'}
    recovered={(r['turn'],r.get('attempt',1)) for r in journal.rows if r['kind']=='recovery'}
    if recovering-recovered:raise AmbiguousToolOutcome('Interrupted context repair requires reconciliation')
    if missing:
        key=next(iter(missing));start=starts[key]
        if key[0]!=turn or start['name']!='user.wait':raise AmbiguousToolOutcome('Only an exact service-attested wait can be reconciled')
        calls=[c for r in journal.rows if r.get('turn')==turn for c in r.get('message',{}).get('tool_calls',[]) if c['id']==key[1]]
        if len(calls)!=1 or loads(calls[0]['function']['arguments'])!={'contact_id':receipt['contact_id']}:
            raise AmbiguousToolOutcome('Wait arguments differ from the controller receipt')
        value={'accepted':True,'recorded':True,'waiting_user':True,'receipt_id':receipt['receipt_id'],
            'contact_id':receipt['contact_id'],'reconciled_from_controller':True}
        journal.append({'kind':'tool_result','turn':turn,'call_id':key[1],'name':'user.wait','error':False,
            'terminal':True,'waiting_user':True,'decision':None,'reconciled_wait_receipt':receipt['receipt_id'],
            'message':{'role':'tool','tool_call_id':key[1],'name':calls[0]['function']['name'],'content':json.dumps(value)}})
    observed=[r for r in journal.rows if r.get('turn')==turn and r['kind']=='tool_result' and r.get('name')=='user.wait' and r.get('waiting_user')]
    if not observed:raise AmbiguousToolOutcome('No matching wait result in checkpoint')
    value=loads(observed[-1]['message']['content'])
    if value.get('receipt_id')!=receipt['receipt_id']:raise PermissionError('Checkpoint wait receipt differs')
    previous=journal.final(turn)
    text=json.dumps({'waiting_user':True})
    if previous:
        if loads(previous['text'])!={'waiting_user':True}:raise PermissionError('Completed gate cannot become a user wait')
    else:journal.append({'kind':'final','turn':turn,'text':text,'reconciled_wait_receipt':receipt['receipt_id']})
    return {'wait_checkpoint_confirmed':True,'receipt_id':receipt['receipt_id']}
