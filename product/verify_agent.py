"""P5 HTTP-model substitute and end-to-end verifier. Run only against an isolated demo stack."""
import argparse
import http.cookiejar
import json
import os
from pathlib import Path
import re
import ssl
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.request import Request, build_opener, HTTPCookieProcessor
from uuid import uuid4

ROOT = Path(__file__).resolve().parent
BASE = os.getenv("PRODUCT_TEST_BASE", "https://127.0.0.1:13446")
COMPOSE = ["docker", "compose", "--project-directory", str(ROOT)]


def require_p8_target():
    """An explicit P8 project name never authorizes main-workbench requests."""
    if not os.getenv("COMPOSE_PROJECT_NAME", "").startswith("tax-product-p8"):
        return
    from urllib.parse import urlsplit
    target=urlsplit(BASE)
    assert target.scheme=="https" and target.hostname in ("localhost","127.0.0.1") and target.port!=3443, "P8 HTTPS isolation required"
    files=["-f",str(ROOT/"compose.yaml"),"-f",str(ROOT/"compose.https.yaml")]
    port=subprocess.check_output(COMPOSE+files+["port","gateway","443"]).decode().strip().rsplit(":",1)[-1]
    assert str(target.port)==port,"P8 URL must match this project's gateway"


def fake_decision(payload):
    current = json.loads(payload["messages"][-1]["content"])
    text = current["message"]
    evidence = current["evidence"]
    requirements = current["requirements"]
    scenario = re.search(r"case=([a-z_]+)", text)
    case = scenario[1] if scenario else "ask"
    decision = {"kind": "ask", "message": "请提供要核验的发票编号和需要补全的内容。"}
    if case == "bad_json":
        return "{"
    if case == "unknown_tool":
        return json.dumps({"kind": "tool", "tool": "confirm_invoice", "arguments": {}})
    if case == "scope_args":
        return json.dumps({"kind": "tool", "tool": "list_invoices", "arguments": {"enterprise_id": "C002"}})
    if case == "details":
        if not evidence:
            return json.dumps({"kind":"tool","tool":"list_invoices",
                "arguments":{"seller":"扩展销售方","item_name":"办公测试"},
                "requirements":[{"requirement_id":"details","kind":"invoices",
                                 "filters":{"seller":"扩展销售方","item_name":"办公测试"}}]})
        return json.dumps({"kind":"answer","message":"筛选结果已核对。",
            "answer_items":[{"requirement_id":"details","evidence_ids":[evidence[-1]["evidence_id"]],"value_key":"records"}]})
    if case in ("proposal", "wrong_target", "viewer"):
        return json.dumps({"kind": "propose", "arguments": {
            "invoice_id": "F002" if case == "wrong_target" else "F001",
            "changes": {"note": "测试补全"}}})
    if case == "resume":
        return json.dumps({"kind": "propose", "arguments": {
            "invoice_id": current.get("pending_invoice") or "F999", "changes": {"note": "测试补全"}}})
    plan = [("list_invoices", {}), ("list_bank_transactions", {}), ("search_rules", {"query": "发票用途"})]
    if case == "missing_bank":
        plan = [("list_invoices", {}), ("search_rules", {"query": "发票用途"})]
    if case in ("forged", "oral"):
        plan = [("list_invoices", {})]
    if case in ("summary", "missing_bank", "forged", "oral", "duplicate", "stale_evidence", "wrong_direction"):
        if case == "wrong_direction":
            plan = [("list_invoices", {}), ("list_bank_transactions", {"direction":"expense"}),
                    ("search_rules", {"query":"发票用途"})]
        if len(evidence) < len(plan):
            tool, args = plan[len(evidence)]
            decision = {"kind": "tool", "tool": tool, "arguments": args}
        else:
            mapping = {"invoice_total": ("list_invoices", "invoice_cents"),
                       "income_total": ("list_bank_transactions", "income_cents"),
                       "expense_total": ("list_bank_transactions", "expense_cents"),
                       "rules": ("search_rules", "rules")}
            items = []
            for req in requirements:
                tool, key = mapping[req["kind"]]
                receipts = [p["evidence_id"] for p in evidence if p["tool"] == tool]
                if receipts:
                    items.append({"requirement_id": req["requirement_id"], "value_key": key, "evidence_ids": receipts})
            decision = {"kind": "answer", "message": "核对完成。",
                        "answer_items": items}
            if case == "forged":
                decision["message"] = "银行收入 99999999 元。"
            if case == "oral":
                decision["message"] = "已保存发票。"
            if case == "duplicate" and items:
                decision["answer_items"] = items + [items[0]]
            if case == "stale_evidence":
                decision["answer_items"] = [{**item,"evidence_ids":["previous-turn-proof"]} for item in items]
    return json.dumps(decision, ensure_ascii=False)


def serve():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            body = json.dumps({"choices": [{"message": {"content": fake_decision(payload)}}]}, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass
    ThreadingHTTPServer(("0.0.0.0", 9000), Handler).serve_forever()


def request(client, path, body=None, expected=200, headers=None, method=None):
    req = Request(BASE + path, data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
        method=method or ("POST" if body is not None else "GET"))
    try:
        with client.open(req, timeout=780) as response:
            raw = response.read().decode()
            assert response.status == expected, (path, response.status)
            return raw
    except HTTPError as error:
        assert error.code == expected, (path, error.code, expected)
        return error.read().decode()


def login(user):
    config = dict(line.split("=",1) for line in (ROOT/".env").read_text().splitlines()
                  if "=" in line and not line.startswith("#"))
    client = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
    request(client,"/api/session",{"username":user,"password":config["PRODUCT_"+user.upper()+"_PASSWORD"]})
    return client


def turn(client, text, session=None, enterprise="C001", period="2026-09"):
    raw = request(client, "/api/enterprises/"+enterprise+"/chat",
                  {"session_id":session or str(uuid4()),"period":period,"message":text})
    entries = []
    for frame in raw.split("\n\n"):
        lines = frame.splitlines()
        name = next((line[7:] for line in lines if line.startswith("event: ")),None)
        value = next((line[6:] for line in lines if line.startswith("data: ")),None)
        if name and value:
            entries.append({"event":name,"data":json.loads(value)})
    assert entries and entries[-1]["event"] == "done", entries
    assert "p5-test-only-model-key" not in raw
    return entries


def has(events, name, code=None):
    return any(e["event"]==name and (code is None or e["data"].get("code")==code) for e in events)


def check(condition, label):
    assert condition, label
    print("PASS " + label, flush=True)


def verify():
    require_p8_target()
    alpha, beta = login("alpha"), login("beta")
    events=turn(alpha,"case=summary 请查发票合计、银行收入、银行支出和规则")
    check(events[-1]["data"]["status"]=="succeeded","invoice/bank/rule evidence and program-rendered amounts")
    answer=next(e["data"]["message"] for e in events if e["event"]=="answer")
    check("1,234.56" in answer and "0.00" in answer and "条款" in answer,"amount and citation accuracy")
    for case, code in [("missing_bank","answer_items_incomplete"),("forged","amount_unverified"),
                       ("oral","proposal_evidence_missing"),("bad_json","invalid_model_response"),("unknown_tool","unknown_tool"),
                       ("duplicate","answer_items_incomplete"),("stale_evidence","evidence_mismatch"),
                       ("wrong_direction","direction_mismatch"),("scope_args","invalid_arguments")]:
        result=turn(alpha,"case="+case+" 请查发票合计、银行收入、银行支出和规则")
        check(has(result,"status",code) and result[-1]["data"]["status"]=="incomplete",case+" rejects false success")
        check(has(result,"error","step_limit"),case+" terminates within six decisions")
    result=turn(alpha,"case=wrong_target 要改 F001 备注为测试补全")
    check(has(result,"status","proposal_target_unverified") and not has(result,"proposal"),"ambiguous or wrong proposal target")
    session=str(uuid4())
    check(has(turn(alpha,"请检查 F001 缺什么",session),"answer"),"ask for missing input")
    check(has(turn(alpha,"顺便问一下规则",session),"answer"),"interruption preserves pending target")
    result=turn(alpha,"case=resume 备注填测试补全",session)
    check(has(result,"proposal"),"resume binds user-provided pending invoice")
    data=next(e["data"] for e in result if e["event"]=="proposal")
    target="/api/enterprises/C001/invoices/F001"
    before=json.loads(request(alpha,target))
    key={"Idempotency-Key":str(uuid4()),"X-Chat-Scope":session}
    path=target+"/proposals/"+data["proposal"]["proposal_id"]+"/confirm"
    first=json.loads(request(alpha,path,{"confirmed":True},headers=key))
    second=json.loads(request(alpha,path,{"confirmed":True},headers=key))
    after=json.loads(request(alpha,target))
    check(first==second and after==first["after"] and after["revision"]==before["revision"]+1,
          "explicit confirmation, same-key replay, independent readback")
    result=turn(beta,"case=viewer F001 备注填测试补全")
    check(any(e["event"]=="tool_result" and e["data"]["result"].get("error",{}).get("code")=="http_403"
              for e in result) and not has(result,"proposal"),"viewer cannot create proposal")
    request(beta,"/api/enterprises/C002/chat",{"session_id":str(uuid4()),"period":"2026-09","message":"查发票"},expected=404)
    print("PASS unauthorized enterprise is 404",flush=True)
    result=turn(alpha,"case=proposal F001 备注填测试补全",session)
    pending=next(e["data"] for e in result if e["event"]=="proposal")
    result=turn(alpha,"改为 C002 企业",session)
    check(has(result,"error","scope_conflict") and not has(result,"tool_call"),"scope conflict before model or business tool")
    request(alpha,target+"/proposals/"+pending["proposal"]["proposal_id"]+"/confirm",{"confirmed":True},
            expected=409,headers={"Idempotency-Key":str(uuid4()),"X-Chat-Scope":session})
    print("PASS old pending proposal rejected by Java after scope conflict",flush=True)
    # No identity or scope parameters accepted from a browser-generated history.
    request(alpha,"/api/enterprises/C001/chat",{"session_id":str(uuid4()),"period":"bad","message":"查发票"},expected=400)
    print("PASS period validation",flush=True)
    program = """import os,httpx
r=httpx.post('http://business:8080/internal/agent/list_invoices',
headers={'Authorization':'Bearer '+os.environ['AGENT_TOKEN']},
json={'call_token':'forged-or-expired','arguments':{}})
assert r.status_code==401,r.status_code
print('PASS internal credential alone cannot impersonate a user')
"""
    subprocess.run(COMPOSE+["exec","-T","agent","python","-c",program],check=True)
    logs = subprocess.check_output(COMPOSE+["logs","--no-color","agent","business","model-stub"],stderr=subprocess.STDOUT).decode(errors="replace")
    config=dict(line.split("=",1) for line in (ROOT/".env").read_text().splitlines() if "=" in line and not line.startswith("#"))
    secrets=[v for k,v in config.items() if len(v)>=12 and any(x in k for x in ("TOKEN","PASSWORD","API_KEY"))]
    check(not any(secret in logs for secret in secrets+["p5-test-only-model-key"]),"credentials absent from service logs")
    active=json.loads(request(alpha,"/api/enterprises/C001/chat/tasks/active?period=2026-09"))
    if active.get("task"):
        request(alpha,"/api/enterprises/C001/chat/tasks/"+active["task"]["task_id"]+"/cancel",
                {"expected_revision":active["task"]["revision"]})
    print("P5_SCRIPTED_VERIFICATION_PASSED",flush=True)


def unconfigured():
    require_p8_target()
    alpha=login("alpha")
    request(alpha,"/api/enterprises/C001/chat",
            {"session_id":str(uuid4()),"period":"2026-09","message":"查询 F001"},expected=503)
    request(alpha,"/api/enterprises/C001/invoices/F001")
    print("PASS unconfigured chat returns 503; invoice API remains available")


def details():
    require_p8_target()
    import io
    from openpyxl import Workbook
    import verify_import as imports
    alpha=login("alpha")
    book=Workbook()
    sheet=book.active
    sheet.append(["invoice_id","accounting_period","purpose","note","amount_cents","seller","item_name"])
    invoice="P5-"+uuid4().hex[:12]
    amount="9007199254740993"
    sheet.append([invoice,"2026-09",None,"扩展导入",amount,"扩展销售方","办公测试"])
    buffer=io.BytesIO()
    book.save(buffer)
    job=imports.upload(alpha,buffer.getvalue())
    path=imports.IMPORTS+"/"+job["job_id"]
    preview=imports.wait_for(alpha,path,"ready")
    assert preview["preview"]["rows"][0]["amount_cents"]==amount
    imports.confirm(alpha,path)
    result=turn(alpha,"case=details 按销售方和项目筛选发票")
    data=next(e["data"]["result"]["data"] for e in result if e["event"]=="tool_result")
    check(result[-1]["data"]["status"]=="succeeded" and len(data["records"])==1
          and data["records"][0]["amount_cents"]==amount,"seven-column import, seller/item filters, exact bigint cents")
    # Legacy P4 imports intentionally have no amount. A total must not silently treat them as zero.
    legacy=imports.xlsx([("P5-"+uuid4().hex[:12],"2026-09",None,"缺少金额")])
    job=imports.upload(alpha,legacy)
    path=imports.IMPORTS+"/"+job["job_id"]
    imports.wait_for(alpha,path,"ready")
    imports.confirm(alpha,path)
    result=turn(alpha,"case=summary 请查发票合计、银行收入、银行支出和规则")
    check(has(result,"status","invoice_amount_unknown") and result[-1]["data"]["status"]=="incomplete",
          "legacy missing amount blocks a complete total")
    sheet.cell(2,5,"-1")
    invalid=io.BytesIO()
    book.save(invalid)
    job=imports.upload(alpha,invalid.getvalue())
    errors=imports.wait_for(alpha,imports.IMPORTS+"/"+job["job_id"],"invalid")
    check(bool(errors["preview"]["errors"]),"invalid cents rejected with row error")


def live():
    require_p8_target()
    config=dict(line.split("=",1) for line in (ROOT/".env").read_text().splitlines() if "=" in line and not line.startswith("#"))
    if not config.get("PRODUCT_MODEL_BASE_URL") or not config.get("PRODUCT_MODEL_NAME"):
        raise RuntimeError("Set the isolated stack model configuration in product/.env before --live")
    actual=json.loads(subprocess.check_output(COMPOSE+["exec","-T","agent","python","-c",
        "import os,json;print(json.dumps({'model':os.environ.get('PRODUCT_MODEL_NAME',''), 'evidence_dir':os.environ.get('PRODUCT_AGENT_EVIDENCE_DIR','')}))"]))
    assert actual["model"] == config["PRODUCT_MODEL_NAME"] and actual["model"] != "product-scripted-verifier", "Live model configuration mismatch"
    assert actual["evidence_dir"].startswith("/embedding-cache/") and ".." not in actual["evidence_dir"], "Enable raw response collection in the isolated agent"
    alpha=login("alpha")
    folder=ROOT.parent/"data"/("agent-live-"+time.strftime("%Y%m%d-%H%M%S"))
    folder.mkdir(parents=True)
    session=str(uuid4())
    scenarios=[
        ("B24-01","请汇总本月发票合计、银行收入、银行支出，并给出用途填写的规则依据。"),
        ("B24-02","检查 F001 的用途和备注，有缺项先问我。"),
        ("B24-03","先查本月银行收入，再继续刚才的 F001 补全。"),
        ("B24-04","F001 用途填测试用途，备注填测试补全，请生成建议供我确认。"),
        ("B34-A","我刚才说的发票是 F001，请把备注建议改成测试补全，等我确认。"),
        ("B34-C","还是 F001，先不要保存，告诉我现在有没有保存。")]
    results=[]
    for case,text in scenarios:
        try:
            events=turn(alpha,text,session)
            record={"input":text,"events":events}
            proposals=[e["data"] for e in events if e["event"]=="proposal"]
            accepted=events[-1]["data"]["status"] in ("succeeded","needs_input","pending_confirmation")
            if case in ("B24-04","B34-A"):
                expected_changes={"purpose":"测试用途","note":"测试补全"} if case=="B24-04" else {"note":"测试补全"}
                accepted=bool(proposals and proposals[0]["invoice_id"]=="F001" and
                    proposals[0]["proposal"]["changes"]==expected_changes and
                    all(v in text for v in proposals[0]["proposal"]["changes"].values()))
            if case=="B24-01":
                answer=" ".join(e["data"].get("message","") for e in events if e["event"]=="answer")
                accepted=all(term in answer for term in ("发票合计","银行收入","银行支出","条款"))
            if case in ("B24-02","B24-03"):
                expected_tool="check_purpose" if case=="B24-02" else "list_bank_transactions"
                accepted=any(e["event"]=="tool_result" and e["data"]["tool"]==expected_tool
                             and e["data"]["result"]["status"]=="succeeded" for e in events) and accepted
            if case in ("B24-04","B34-A") and accepted:
                data=proposals[0]
                target="/api/enterprises/C001/invoices/F001"
                before=json.loads(request(alpha,target))
                other=json.loads(request(alpha,target.replace("C001","C002")))
                beta=login("beta")
                other_tenant=json.loads(request(beta,target))
                path=target+"/proposals/"+data["proposal"]["proposal_id"]+"/confirm"
                key={"Idempotency-Key":str(uuid4()),"X-Chat-Scope":session}
                receipt=json.loads(request(alpha,path,{"confirmed":True},headers=key))
                replay=json.loads(request(alpha,path,{"confirmed":True},headers=key))
                after=json.loads(request(alpha,target))
                accepted=(receipt==replay and after==receipt["after"] and after["revision"]==before["revision"]+1
                          and other==json.loads(request(alpha,target.replace("C001","C002")))
                          and other_tenant==json.loads(request(beta,target)))
                record["confirmation"]={"mode":"explicit test-client confirmation","receipt":receipt,"readback":after,
                                         "same_key_replay":receipt==replay}
            if case=="B34-C":
                accepted=(events[-1]["data"]["status"]=="succeeded" and
                          any(e["event"]=="tool_result" and e["data"]["tool"]=="get_operation"
                              and e["data"]["result"]["status"]=="succeeded" for e in events) and not proposals)
            (folder/(case+".json")).write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding="utf-8")
            results.append({"case":case,"status":"passed" if accepted else "failed","final":events[-1]["data"]})
        except Exception as error:
            results.append({"case":case,"status":"failed","error":type(error).__name__})
    (folder/"summary.json").write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding="utf-8")
    agent_id=subprocess.check_output(COMPOSE+["ps","-q","agent"]).decode().strip()
    subprocess.run(["docker","cp",agent_id+":"+actual["evidence_dir"],str(folder/"raw-model")],check=True)
    print(json.dumps({"folder":str(folder),"results":results},ensure_ascii=False,indent=2))
    return 0 if all(row["status"]=="passed" for row in results) else 1


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--serve",action="store_true")
    parser.add_argument("--live",action="store_true")
    parser.add_argument("--unconfigured",action="store_true")
    parser.add_argument("--details",action="store_true")
    options=parser.parse_args()
    if options.serve:
        serve()
    elif options.live:
        sys.exit(live())
    elif options.unconfigured:
        unconfigured()
    elif options.details:
        details()
    else:
        verify()
