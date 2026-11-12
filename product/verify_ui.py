"""P7 browser acceptance. Use only disposable tax-product-p7 data; actual HTTP/API and UI."""
import json,os,subprocess,time
from pathlib import Path
from uuid import uuid4
from playwright.sync_api import sync_playwright,expect
import verify_agent as v

ROOT=Path(__file__).resolve().parent
FILES=['-f',str(ROOT/'compose.yaml'),'-f',str(ROOT/'compose.https.yaml'),'-f',str(ROOT/'compose.agent-test.yaml'),'-f',str(ROOT/'compose.memory-test.yaml')]
COMPOSE=['docker','compose','--project-directory',str(ROOT)]
RESULTS=[]
def check(name,condition=True):
    assert condition,name
    RESULTS.append({'case':name,'status':'passed'})
    print('PASS '+name,flush=True)
def reconfigure(unconfigured=False):
    args=COMPOSE+FILES+(['-f',str(ROOT/'compose.agent-unconfigured.yaml')] if unconfigured else [])
    subprocess.run(args+['up','-d','--no-deps','--force-recreate','--wait','agent'],check=True)
def verify():
    v.require_p8_target()
    assert os.environ.get('COMPOSE_PROJECT_NAME','').startswith(('tax-product-p7','tax-product-p8')),'Disposable P7 project required'
    folder=ROOT.parent/'data'/('p7-ui-'+time.strftime('%Y%m%d-%H%M%S'));folder.mkdir(parents=True)
    config=dict(line.split('=',1) for line in (ROOT/'.env').read_text().splitlines() if '=' in line)
    alpha=v.login('alpha');base='/api/enterprises/C001/chat'
    def request(path,body=None,expected=200,method=None,headers=None):return json.loads(v.request(alpha,path,body,expected,headers,method) or 'null')
    old=request(base+'/tasks/active?period=2026-09')
    if old.get('task'):request(base+'/tasks/'+old['task']['task_id']+'/cancel',{'expected_revision':old['task']['revision']})
    for m in request(base+'/memories'):request(base+'/memories/'+m['memory_id'],{'expected_revision':m['revision']},method='DELETE')
    errors=[];confirm_requests=[]
    with sync_playwright() as pw:
        browser=pw.chromium.launch(channel='msedge',headless=True)
        context=browser.new_context(viewport={'width':1440,'height':900})
        page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
        page.on('request',lambda r:confirm_requests.append(json.loads(r.post_data or '{}')|{'key':r.headers.get('idempotency-key')}) if '/confirm' in r.url and '/proposals/' in r.url else None)
        page.set_default_timeout(20000)
        def login():
            page.get_by_label('账户',exact=True).fill('alpha');page.get_by_label('密码',exact=True).fill(config['PRODUCT_ALPHA_PASSWORD']);page.get_by_role('button',name='登录',exact=True).click();expect(page.get_by_role('button',name='退出登录')).to_be_visible()
        def send(text,final=None):
            box=page.get_by_role('textbox',name='查询或补全要求',exact=True);box.fill(text);box.press('Enter')
            expect(box).to_be_enabled(timeout=90000)
            if final:expect(page.get_by_test_id('turn-status')).to_have_text(final,timeout=90000)
        def cancel():
            button=page.get_by_role('button',name='取消当前任务',exact=True)
            if button.count():button.click();expect(page.get_by_text('确定取消当前任务？未保存的建议将失效。')).to_be_visible();page.get_by_role('button',name='确定取消',exact=True).click();expect(button).to_have_count(0)
        page.goto(v.BASE);login();page.reload();expect(page.get_by_role('button',name='退出登录')).to_be_visible();check('session refresh without login')
        page.get_by_role('button',name='退出登录').click();page.reload();expect(page.get_by_role('button',name='登录',exact=True)).to_be_visible();login();check('logout and refresh return login')
        send('case=summary 请查发票合计、银行收入、银行支出和规则','已回答')
        expect(page.get_by_text('需求清单',exact=True).first).to_be_visible();expect(page.get_by_text('发票查询',exact=True).first).to_be_visible()
        link=page.get_by_role('button',name='依据：发票查询',exact=False).first;expect(link).to_be_visible();link.click();expect(page.locator('.tool-card.selected')).to_be_visible();check('compound query requirements timeline tools evidence and answer navigation')
        send('case=forged 查询发票合计','未完成');expect(page.get_by_text('金额未经工具核实，已拦截',exact=True).first).to_be_visible();check('forged amount guard explained in Chinese');cancel()
        send('case=ask 请核验 F001，缺少备注请追问','需要你补充');send('case=p6_bank 顺便查银行收入','已回答')
        page.reload();expect(page.get_by_text('当前任务',exact=True)).to_be_visible();expect(page.get_by_text('F001',exact=True).first).to_be_visible();check('pending task survives page refresh')
        send('case=resume 备注填测试补全','待你确认')
        card=page.get_by_role('article',name='待确认的发票建议');expect(card).to_be_visible()
        page.get_by_role('button',name='收起建议',exact=True).click();page.get_by_role('button',name='展开建议',exact=True).click();expect(card.get_by_text('测试补全',exact=True).last).to_be_visible();check('collapse preserves pending proposal')
        page.get_by_label('对话月份',exact=True).fill('2026-08');expect(page.get_by_text('跨月不可确认，请切回任务月份。')).to_be_visible();expect(page.get_by_role('button',name='确认保存',exact=True)).to_be_disabled();check('other month confirmation disabled with reason')
        page.get_by_label('对话月份',exact=True).fill('2026-09');page.reload();expect(page.get_by_role('button',name='确认保存',exact=True)).to_be_disabled();page.get_by_role('button',name='恢复当前任务',exact=True).click();expect(page.get_by_role('button',name='确认保存',exact=True)).to_be_enabled()
        invoice=request('/api/enterprises/C001/invoices/F001');page.get_by_role('button',name='确认保存',exact=True).click();expect(page.get_by_text('保存与回查通过。',exact=True)).to_be_visible();expect(page.get_by_text('系统回执',exact=True)).to_be_visible();check('resume then explicit confirmation and readback receipt')
        audit=request('/api/enterprises/C001/invoices/F001/audit')[0];key=confirm_requests[-1]['key'];assert audit['after']['revision']==invoice['revision']+1
        # Repeat the identical browser confirmation using its exact cookie and scope header via API.
        cookies=context.cookies();client=__import__('urllib.request',fromlist=['build_opener']).build_opener()
        scope=subprocess.check_output(COMPOSE+['exec','-T','db','psql','-U','tax_product','-d','tax_product','-At','-c',"select chat_scope_id from invoice_proposal where proposal_id='"+audit['proposal_id']+"'"]).decode().strip()
        headers={'Idempotency-Key':key,'X-Chat-Scope':scope,'Cookie':'; '.join(c['name']+'='+c['value'] for c in cookies)}
        replay=json.loads(v.request(client,'/api/enterprises/C001/invoices/F001/proposals/'+audit['proposal_id']+'/confirm',{'confirmed':True},headers=headers));check('same browser idempotency key returns same operation',replay==audit)
        send('case=ask 请核验 F001，缺少备注请追问','需要你补充');send('case=resume 备注填测试补全','待你确认')
        def interrupt_readback(route):route.abort()
        page.route('**/api/enterprises/C001/invoices/F001',interrupt_readback)
        page.get_by_role('button',name='确认保存',exact=True).click();expect(page.get_by_text('保存结果待核实',exact=True)).to_be_visible()
        page.unroute('**/api/enterprises/C001/invoices/F001',interrupt_readback)
        assert page.get_by_role('button',name='确认保存',exact=True).count()==0 or not page.get_by_role('button',name='确认保存',exact=True).is_enabled()
        page.get_by_role('button',name='核实操作结果',exact=True).click();expect(page.get_by_text('保存结果已核实。',exact=True)).to_be_visible();check('interrupted readback blocks confirmation and reconciles committed operation')
        send('case=p6_pref 以后请简洁回答','需要你补充')
        page.get_by_role('button',name='记为偏好',exact=True).last.click();page.get_by_label('偏好内容',exact=True).fill('P7 简洁偏好');page.get_by_role('button',name='新增偏好',exact=True).click();expect(page.get_by_text('偏好已保存。',exact=True)).to_be_visible()
        memory=page.locator('.memory-card').filter(has_text='P7 简洁偏好');expect(memory).to_be_visible();page.get_by_label('偏好内容',exact=True).fill('P7 编号偏好');memory.get_by_role('button',name='用输入内容更正',exact=True).click();memory=page.locator('.memory-card').filter(has_text='P7 编号偏好');expect(memory).to_be_visible();memory.get_by_role('button',name='查看版本记录',exact=True).click();expect(page.get_by_role('heading',name='偏好版本记录',exact=True)).to_be_visible();memory.get_by_role('button',name='删除偏好',exact=True).click();expect(memory).to_have_count(0);check('message sourced preference create correct audit delete')
        # Change the same task from another API client to force a real optimistic version conflict.
        task=request(base+'/tasks/active?period=2026-09')['task'];request(base+'/tasks/'+task['task_id']+'/correct',{'expected_revision':task['revision'],'question':'P7 并发版本更正'})
        page.get_by_role('button',name='恢复当前任务',exact=True).click();expect(page.get_by_text('版本或确认记录冲突，请重新核验。',exact=True)).to_be_visible();check('409 conflict informs user and refreshes task');cancel();check('task cancellation requires second confirmation')
        page.get_by_role('button',name='收起版本记录',exact=True).click()
        page.reload();expect(page.get_by_role('button',name='退出登录')).to_be_visible()
        send('case=summary 请查发票合计、银行收入、银行支出和规则','已回答')
        page.locator('.scope-panel').evaluate('(el)=>el.scrollTop=0')
        page.screenshot(path=str(folder/'desktop-1440x900.png'),full_page=True)
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth');check('desktop screenshot no horizontal overflow')
        page.set_viewport_size({'width':390,'height':844});page.get_by_role('button',name='对话',exact=True).click();page.screenshot(path=str(folder/'mobile-390x844.png'),full_page=False);assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth');check('mobile screenshot no horizontal overflow')
        page.get_by_role('button',name='过程',exact=True).click();expect(page.get_by_role('heading',name='过程与依据',exact=True)).to_be_visible();assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth');page.get_by_role('button',name='任务',exact=True).click();expect(page.get_by_role('heading',name='企业与任务',exact=True)).to_be_visible();assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth');page.get_by_role('button',name='对话',exact=True).click();check('narrow screen keyboard accessible tabs')
        page.set_viewport_size({'width':1440,'height':900})
        try:
            reconfigure(True);send('查询 F001');expect(page.get_by_test_id('model-status')).to_have_text('模型未配置');expect(page.get_by_text('模型未配置，请配置后重试；业务数据页仍可使用。',exact=True)).to_be_visible();page.get_by_role('button',name='业务数据',exact=True).click();expect(page.get_by_role('heading',name='发票核验与补全',exact=True)).to_be_visible();page.get_by_role('button',name='查询并核验',exact=True).click();check('unconfigured model 503 with working business data')
        finally:reconfigure()
        check('no uncaught browser exceptions',not errors)
        # Invalidate this browser session to exercise genuine expiration without weakening security.
        cookie=next(c['value'] for c in context.cookies() if c['name']=='tax_product_session');subprocess.run(COMPOSE+['exec','-T','redis','redis-cli','DEL','account-session:'+cookie],check=True,stdout=subprocess.DEVNULL)
        page.get_by_role('button',name='查询并核验',exact=True).click();expect(page.get_by_role('button',name='登录',exact=True)).to_be_visible();expect(page.get_by_text('会话已过期，请重新登录。',exact=True)).to_be_visible();check('expired session returns to login')
        browser.close()
    (folder/'results.json').write_text(json.dumps({'results':RESULTS,'browser_errors':errors},ensure_ascii=False,indent=2),encoding='utf-8')
    print('P7_UI_PASSED '+str(folder),flush=True)
if __name__=='__main__':verify()
