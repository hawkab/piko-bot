"""One durable advisory checkpoint for long discovery; never an approval or cap."""
READS={'code.read','code.inspect','code.search','code.search_many','spec.read',
       'spec.search','spec.context','spec.verify','memory.recall','autotests.baseline'}
AUTHORED={'analysis.submit','task.plan','analysis.report','git.local_commit',
          'autotests.submit','autotests.revise','review.submit','pull_request.review'}


def checkpoint(journal,turn,messages):
    rows=[r for r in journal.rows if r.get('turn')==turn]
    if any(r['kind']=='focus_checkpoint' for r in rows):return
    results=[r for r in rows if r['kind']=='tool_result']
    if any(r.get('name') in AUTHORED and not r.get('error') for r in results):return
    reads=sum(r.get('name') in READS for r in results)
    if reads<12:return
    message={'role':'user','content':
        'Progress checkpoint: '+str(reads)+' discovery calls are recorded, with their actual results above. '
        'Consider whether enough evidence exists for the smallest useful draft or your independent review. '
        'If so, create it now and improve it through validation/review. Otherwise identify one concrete remaining gap '
        'and batch the relevant reads. code.inspect(filename=literal_basename) finds paths; code.search searches content. '
        'This is advisory, not a deadline or evidence that checks passed. '
        'Keep unresolved requirements and uncertainty explicit; do not approve merely to stop discovery.'}
    journal.append({'kind':'focus_checkpoint','turn':turn,'discovery_calls':reads,'message':message})
    messages.append(message)
