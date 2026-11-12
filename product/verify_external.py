"""Real-model smoke acceptance on disposable P8 product seed data, not B01-B35 certification."""
import argparse,json,os,subprocess,time
from pathlib import Path
from uuid import uuid4
from urllib.parse import urlsplit
import verify_agent as v
import verify_memory as m
ROOT=Path(__file__).resolve().parent
BASE='/api/enterprises/C001/chat'

def run(rounds):
    assert os.getenv('COMPOSE_PROJECT_NAME','').startswith('tax-product-p8'),'Use disposable tax-product-p8 instance'
    target=urlsplit(v.BASE)
    published=subprocess.check_output(m.COMPOSE+['port','gateway','443']).decode().strip().rsplit(':',1)[-1]
    if target.scheme!='https' or target.hostname not in ('localhost','127.0.0.1') or target.port==3443 or str(target.port)!=published:
        raise RuntimeError('Acceptance URL must match this P8 gateway, never main 3443')
    actual=json.loads(subprocess.check_output(m.COMPOSE+['exec','-T','agent','python','-c',"import os,json; print(json.dumps({'model':os.environ.get('PRODUCT_MODEL_NAME'),'evidence_dir':os.environ.get('PRODUCT_AGENT_EVIDENCE_DIR')}))"]))
    assert actual['model']=='qwen3.8-max' and actual['evidence_dir'].startswith('/embedding-cache/'),'Unexpected model/evidence configuration'
    cfg=dict(s.split('=',1) for s in (ROOT/'.env').read_text(encoding='utf-8-sig').splitlines() if '=' in s and not s.startswith('#'))
    out=ROOT.parent/'data'/('p8-live-qwen3.8-max-'+time.strftime('%Y%m%d-%H%M%S'));out.mkdir(parents=True)
    report={'model':actual['model'],'dataset':'synthetic product seed; not v1.1.0-equivalent fixture','results':[]}
    alpha=v.login('alpha');beta=v.login('beta')
    def call(path,body=None,expected=200,headers=None,method=None):return json.loads(v.request(alpha,path,body,expected,headers,method) or 'null')
    def cleanup():
        active=call(BASE+'/tasks/active?period=2026-09')
        if active.get('task'):
            for op in active.get('unfinished_operations',[]):call(BASE+'/tasks/'+active['task']['task_id']+'/operations/'+op['operation_id']+'/reconcile',{})
            row=call(BASE+'/tasks/active?period=2026-09')['task']
            if row:call(BASE+'/tasks/'+row['task_id']+'/cancel',{'expected_revision':row['revision']})
    def turn(name,text,session):
        row={'case':name,'input':text}
        try:
            raw=v.request(alpha,BASE,{'session_id':session,'period':'2026-09','message':text})
            assert not cfg['PRODUCT_MODEL_API_KEY'] or cfg['PRODUCT_MODEL_API_KEY'] not in raw,'Credential redaction failure'
            (out/(name+'.sse')).write_text(raw,encoding='utf-8')
            events=[]
            for frame in raw.split('\n\n'):
                lines=frame.splitlines();event=next((x[7:] for x in lines if x.startswith('event: ')),None);data=next((x[6:] for x in lines if x.startswith('data: ')),None)
                if event and data:events.append({'event':event,'data':json.loads(data)})
            row['events']=events
        except Exception as error:row['error_type']=type(error).__name__;events=[]
        return row,events
    def save(row,passed):
        row['status']='passed' if passed else 'failed';row['completion']='model' if passed else 'failed'
        (out/(row['case']+'.json')).write_text(json.dumps(row,ensure_ascii=False,indent=2),encoding='utf-8')
        report['results'].append({'case':row['case'],'status':row['status'],'completion':row['completion']})
        (out/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(row['case'],row['status'],flush=True)
    for n in range(1,rounds+1):
        alpha=v.login('alpha');beta=v.login('beta')
        cleanup();session=str(uuid4())
        scenarios=[('B24-01','请汇总本月发票合计、银行收入、银行支出，并给出用途填写的规则依据。'),('B24-02','检查 F001 的用途和备注，有缺项先问我。'),('B24-03','先查本月银行收入，再继续刚才的 F001 补全。'),('B24-04','F001 用途填测试用途，备注填测试补全，请生成建议供我确认。'),('B34-A','我刚才说的发票是 F001，请把备注建议改成测试补全，等我确认。'),('B34-C','还是 F001，先不要保存，告诉我现在有没有保存。')]
        for case,text in scenarios:
            row,events=turn(f'round{n}-{case}',text,session)
            accepted=bool(events and events[-1]['event']=='done' and events[-1]['data'].get('status') in ('succeeded','needs_input','pending_confirmation'))
            proposals=[e['data'] for e in events if e['event']=='proposal']
            if case=='B24-01':
                answer=' '.join(e['data'].get('message','') for e in events if e['event']=='answer')
                accepted=accepted and all(t in answer for t in ('发票合计','银行收入','银行支出','条款'))
            if case in ('B24-02','B24-03'):
                expected='check_purpose' if case=='B24-02' else 'list_bank_transactions'
                accepted=accepted and any(e['event']=='tool_result' and e['data']['tool']==expected and e['data']['result']['status']=='succeeded' for e in events)
            if case in ('B24-04','B34-A'):
                changes={'purpose':'测试用途','note':'测试补全'} if case=='B24-04' else {'note':'测试补全'}
                accepted=bool(proposals and proposals[0]['invoice_id']=='F001' and proposals[0]['proposal']['changes']==changes)
                if accepted:
                    try:
                        target='/api/enterprises/C001/invoices/F001';before=call(target);other=call(target.replace('C001','C002'));tenant=json.loads(v.request(beta,target))
                        headers={'Idempotency-Key':str(uuid4()),'X-Chat-Scope':session};path=target+'/proposals/'+proposals[0]['proposal']['proposal_id']+'/confirm'
                        receipt=call(path,{'confirmed':True},headers=headers);replay=call(path,{'confirmed':True},headers=headers);after=call(target)
                        accepted=receipt==replay and after==receipt['after'] and after['revision']==before['revision']+1 and other==call(target.replace('C001','C002')) and tenant==json.loads(v.request(beta,target))
                        row['confirmation']={'mode':'explicit acceptance-client confirmation','receipt':receipt,'readback':after,'same_key_replay':receipt==replay}
                    except Exception as error:accepted=False;row['confirmation_error_type']=type(error).__name__
            if case=='B34-C':accepted=accepted and events[-1]['data'].get('status')=='succeeded' and not proposals and any(e['event']=='tool_result' and e['data']['tool']=='get_operation' and e['data']['result']['status']=='succeeded' for e in events)
            save(row,accepted)
        cleanup()
    cleanup();session=str(uuid4());row,events=turn('P6-B32-initial','请核验 F001，备注待我补充，请先询问。',session)
    active=call(BASE+'/tasks/active?period=2026-09');pending=active.get('task');save(row,bool(pending and pending['object_id']=='F001' and events and events[-1]['data'].get('status')=='needs_input'))
    source=next((e['data']['message_id'] for e in events if e['event']=='status' and e['data'].get('message_id')),None)
    if source:
        pref=call(BASE+'/memories',{'content':'请用简洁列表回答。','source_message_id':source})
        row,events=turn('P6-B10-created','请说明接下来需要补充的信息。',session)
        task_id=events[-1]['data'].get('task_id') if events else None
        def context_for(task):
            traces=call(BASE+'/tasks/'+task+'/traces');snap=traces[-1]['summary'].get('context_snapshots',[])
            return json.loads(snap[-1]['messages'][-1]['content']) if snap else {}
        context=context_for(task_id) if task_id else {};save(row,any(p['memory_id']==pref['memory_id'] and p['revision']==1 for p in context.get('memories',[])))
        call(BASE+'/memories/'+pref['memory_id']+'/correct',{'content':'请使用编号列表回答。','expected_revision':1})
        subprocess.run(m.COMPOSE+['restart','agent'],check=True,stdout=subprocess.DEVNULL)
        subprocess.run(m.COMPOSE+['-f',str(ROOT/'compose.yaml'),'-f',str(ROOT/'compose.https.yaml'),'-f',str(ROOT/'compose.external-acceptance.yaml'),'up','-d','--no-deps','--wait','agent'],check=True,stdout=subprocess.DEVNULL)
        row,events=turn('P6-B32-restart','请继续原任务，先说明待补充的内容。',str(uuid4()));task_id=events[-1]['data'].get('task_id') if events else None
        context=context_for(task_id) if task_id else {};current=call(BASE+'/tasks/active?period=2026-09').get('task');save(row,bool(pending and current and current['task_id']==pending['task_id'] and current['object_id']=='F001' and any(p['memory_id']==pref['memory_id'] and p['revision']==2 for p in context.get('memories',[]))))
        call(BASE+'/memories/'+pref['memory_id'],{'expected_revision':2},method='DELETE')
        row,events=turn('P6-B10-deleted','请说明下一步。',session);task_id=events[-1]['data'].get('task_id') if events else None;context=context_for(task_id) if task_id else {}
        save(row,bool(context and all(p['memory_id']!=pref['memory_id'] for p in context.get('memories',[])) and all(m['message_id']!=source for m in context.get('history',[]))))
    subprocess.run(['docker','cp','tax-product-p8-agent-1:'+actual['evidence_dir'],str(out/'raw-model')],check=True)
    cleanup();report['passed']=sum(r['status']=='passed' for r in report['results']);report['failed']=sum(r['status']=='failed' for r in report['results']);report['program_fallback']=0
    (out/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print('P8_EXTERNAL_EVIDENCE',str(out),report['passed'],report['failed'],flush=True)
    return 0 if not report['failed'] else 1
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--rounds',type=int,default=3);args=p.parse_args();assert 1<=args.rounds<=3;raise SystemExit(run(args.rounds))
