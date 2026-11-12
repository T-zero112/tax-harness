"""P8-only integration checks for verified operation visibility."""
import json,os
from uuid import uuid4
import verify_agent as v
import verify_memory as m

def run():
 v.require_p8_target()
 assert os.getenv('COMPOSE_PROJECT_NAME','').startswith('tax-product-p8')
 a=v.login('alpha');b=v.login('beta');base=m.BASE
 def call(client,path,body=None,expected=200,headers=None):return json.loads(v.request(client,path,body,expected,headers))
 old=m.active(a)
 if old.get('task'):call(a,base+'/tasks/'+old['task']['task_id']+'/cancel',{'expected_revision':old['task']['revision']})
 session=str(uuid4());events=v.turn(a,'case=proposal F001 备注填测试补全',session)
 proposal=next(e['data']['proposal']['proposal_id'] for e in events if e['event']=='proposal')
 path='/api/enterprises/C001/invoices/F001/proposals/'+proposal+'/confirm'
 call(a,path,{'confirmed':True},headers={'Idempotency-Key':str(uuid4()),'X-Chat-Scope':session})
 def context(client,enterprise='C001',period='2026-09'):
  es=v.turn(client,'请说明下一步',str(uuid4()),enterprise,period)
  task=es[-1]['data']['task_id'];rows=call(client,'/api/enterprises/'+enterprise+'/chat/tasks/'+task+'/traces')
  return json.loads(rows[-1]['summary']['context_snapshots'][-1]['messages'][-1]['content'])
 own=context(a);assert any(str(x['proposal_id'])==proposal and x['state']=='confirmed' for x in own['recent_operations'])
 events=v.turn(a,'case=p8_operation F001 有没有保存',str(uuid4()))
 assert events[-1]['data']['status']=='succeeded'
 proofs=[e['data'] for e in events if e['event']=='tool_result' and e['data']['tool']=='get_operation']
 assert len(proofs)==1 and str(proofs[0]['result']['data']['operation']['proposal_id'])==proposal
 assert proofs[0]['result']['data']['operation']['state']=='confirmed'
 assert all(str(x['proposal_id'])!=proposal for x in context(b)['recent_operations'])
 assert all(str(x['proposal_id'])!=proposal for x in context(a,'C002')['recent_operations'])
 assert all(str(x['proposal_id'])!=proposal for x in context(a,'C001','2026-08')['recent_operations'])
 print('PASS R2 own confirmed operation reachable; cross-tenant, enterprise and period isolated',flush=True)
if __name__=='__main__':run()
