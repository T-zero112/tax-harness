"""Smoke test the product through its public Next.js entry point."""
import http.cookiejar
import json
import sys
import math
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener
from urllib.parse import urlencode
from uuid import uuid4


BASE = os.getenv("PRODUCT_TEST_BASE", "http://127.0.0.1:3100")
config = dict(line.split("=", 1) for line in Path(__file__).with_name(".env").read_text().splitlines()
              if line.strip() and not line.startswith("#"))


def call(client, path, *, method="GET", body=None, expected=200, headers=None):
    request = Request(BASE + path, method=method, data=None if body is None else json.dumps(body).encode(),
                      headers={"Content-Type": "application/json", "X-Tenant-Id": "beta", **(headers or {})})
    try:
        with client.open(request, timeout=15) as response:
            assert response.status == expected, (path, response.status)
            return response.read().decode()
    except HTTPError as error:
        assert error.code == expected, (path, error.code)


anonymous = build_opener()
call(anonymous, "/")
call(anonymous, "/api/enterprises", expected=401)
call(anonymous, "/api/session", method="POST", body={"username": "alpha", "password": "incorrect"}, expected=401)
clients = {}
for tenant, expected_name in [("alpha", "甲租户示例企业"), ("beta", "乙租户示例企业")]:
    client = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
    session = call(client, "/api/session", method="POST",
                   body={"username": tenant, "password": config[f"PRODUCT_{tenant.upper()}_PASSWORD"]})
    assert json.loads(session)["tenant_id"] == tenant
    expected_enterprises = [{"enterprise_id": "C001", "name": expected_name}]
    if tenant == "alpha":
        expected_enterprises.append({"enterprise_id": "C002", "name": "甲租户第二企业"})
    assert json.loads(call(client, "/api/enterprises?tenant_id=beta")) == expected_enterprises
    call(client, "/api/enterprises/C999/probe", method="POST", expected=404)
    events = call(client, "/api/enterprises/C001/probe", method="POST")
    assert "event: status\n" in events and "event: done\n" in events
    clients[tenant] = client

alpha, beta = clients["alpha"], clients["beta"]

if "--p3" in sys.argv:
    rules = "/api/enterprises/C002/rules"
    invoice = "/api/enterprises/C002/invoices/F001"
    def get(client, path, **kwargs):
        return json.loads(call(client, path, **kwargs))
    def send(client, path, body=None, **kwargs):
        return get(client, path, method="POST", body=body, **kwargs)
    def search(client, path, query, period="2026-09"):
        return get(client, path + "/search?" + urlencode({"period": period, "query": query}))
    def state(rule, action):
        return send(alpha, rules + "/" + rule["citation"]["rule_id"] + "/" + action)
    def draft(code, content, required, start="2026-09", until=None):
        return {"code": code, "version": str(uuid4()), "source": "P3 验收企业规则",
                "clause": "1.1", "content": content, "required_fields": required,
                "effective_from": start, "effective_until": until}
    def approve(proposal, expected=200):
        return call(alpha, invoice + "/proposals/" + proposal["proposal_id"] + "/confirm",
                    method="POST", body={"confirmed": True}, headers={"Idempotency-Key": str(uuid4())},
                    expected=expected)

    base = search(alpha, rules, "发票用途")
    assert len(base) == 1
    original = base[0]["rule"]
    assert search(alpha, rules, "发票用途", "2026-08") == []
    alpha_rules = {hit["rule"]["citation"]["rule_id"] for hit in search(alpha, "/api/enterprises/C001/rules", "发票用途")}
    beta_rules = {hit["rule"]["citation"]["rule_id"] for hit in search(beta, "/api/enterprises/C001/rules", "发票用途")}
    assert alpha_rules and beta_rules and alpha_rules.isdisjoint(beta_rules)
    assert original["citation"]["rule_id"] not in alpha_rules | beta_rules
    call(beta, rules, expected=404)
    call(beta, "/api/enterprises/C001/rules", method="POST", body=draft("forbidden", "发票用途", ["purpose"]), expected=403)
    call(alpha, "/api/enterprises/C001/rules/" + original["citation"]["rule_id"] + "/retire", method="POST", expected=404)
    future = send(alpha, rules, draft("future", "十月专项发票核验", ["note"], "2026-10", "2026-11"))
    assert future["state"] == "draft"
    assert len(search(alpha, rules, "十月专项", "2026-10")) == 1
    state(future, "publish")
    for period, present in [("2026-09", False), ("2026-10", True), ("2026-11", False)]:
        hits = search(alpha, rules, "十月专项", period)
        assert any(hit["rule"]["citation"] == future["citation"] for hit in hits) == present
    state(future, "retire")

    unrelated = send(alpha, rules, draft("semantic-control", "甜西瓜是夏季水果，适合切片食用。", original["required_fields"]))
    state(unrelated, "publish")
    query = "这笔采购为何要写明业务事由"
    hits = search(alpha, rules, query)
    assert len(hits) == 2 and all(not hit["exact_match"] and math.isfinite(hit["semantic_score"]) for hit in hits)
    assert hits[0]["rule"]["citation"] == original["citation"] and hits[0]["semantic_score"] > hits[1]["semantic_score"]
    literal = search(alpha, rules, "甜西瓜")
    assert literal[0]["rule"]["citation"] == unrelated["citation"] and literal[0]["exact_match"]
    state(unrelated, "retire")

    before = get(alpha, invoice)
    review = send(alpha, invoice + "/review")
    assert review["rule_ref"] == original["citation"]
    change = {"note": "P3 版本验收 " + str(uuid4())}
    pending = send(alpha, invoice + "/proposals", change)
    updated = send(alpha, rules, draft(original["citation"]["code"], "发票须注明业务用途和采购事由，以便核验。",
                                      ["purpose"] if original["required_fields"] != ["purpose"] else ["note", "purpose"]))
    state(updated, "publish")
    conflict = send(alpha, invoice + "/proposals", change)
    assert conflict["status"] == "manual_review" and conflict["reason"] == "rule_conflict"
    assert {item["rule_id"] for item in conflict["candidates"]} == {
        original["citation"]["rule_id"], updated["citation"]["rule_id"]}
    assert "proposal_id" not in conflict and conflict["review_id"]
    approve(pending, expected=409)
    state(updated, "retire")
    state(original, "retire")
    missing = send(alpha, invoice + "/review")
    assert missing["status"] == "manual_review" and missing["reason"] == "rule_missing"
    assert missing["review_id"] and missing["candidates"] == []
    assert get(alpha, invoice) == before

    current = send(alpha, rules, draft(original["citation"]["code"],
        "发票须填写用途与备注，写明采购支出的业务事由。", ["note", "purpose"]))
    state(current, "publish")
    approve(pending, expected=409)
    proposal = send(alpha, invoice + "/proposals", change)
    assert proposal["rule_ref"] == current["citation"] and proposal["expected_revision"] == before["revision"]
    saved = json.loads(approve(proposal))
    assert saved["rule_ref"] == current["citation"] == get(alpha, invoice + "/audit")[0]["rule_ref"]
    assert saved["after"] == get(alpha, invoice)
    assert saved["after"]["revision"] == before["revision"] + 1
    print("PASS: rule isolation, date bounds, literal and semantic ranking, manual review, version switch and citations")
    print(f"Semantic score: applicable {hits[0]['semantic_score']:.4f}; unrelated {hits[1]['semantic_score']:.4f}")
    print(f"Manual review IDs: {conflict['review_id']}, {missing['review_id']}")
    raise SystemExit

target = "/api/enterprises/C001/invoices/F001"
other = "/api/enterprises/C002/invoices/F001"
call(anonymous, target, expected=401)
before = json.loads(call(alpha, target))
other_before = json.loads(call(alpha, other))
other_audit_before = json.loads(call(alpha, other + "/audit"))
beta_before = json.loads(call(beta, target))
assert other_before["purpose"] == "第二企业用途"
assert beta_before["purpose"] == "乙租户用途"
call(beta, other, expected=404)
review = json.loads(call(alpha, target + "/review", method="POST"))
assert set(review["missing"]) == {field for field in ("purpose", "note") if not (before[field] or "").strip()}
assert review["rule_version"] == "product-invoice-completeness:1"
audit_before = json.loads(call(alpha, target + "/audit"))
if "--snapshot" in sys.argv:
    print(json.dumps({"invoice": before, "audit": audit_before, "other_enterprise": other_before,
                      "other_tenant": beta_before}, ensure_ascii=True, sort_keys=True))
    raise SystemExit
changes = {"purpose": "P2 核验用途", "note": "验收 " + str(uuid4())}
call(beta, target + "/proposals", method="POST", body=changes, expected=403)
call(alpha, target + "/proposals", method="POST", body={"amount": "123"}, expected=400)
proposal = json.loads(call(alpha, target + "/proposals", method="POST", body=changes))
stale = json.loads(call(alpha, target + "/proposals", method="POST", body={"note": "旧版本建议"}))
assert proposal["changes"] == changes and proposal["expected_revision"] == before["revision"]
assert json.loads(call(alpha, target)) == before
assert json.loads(call(alpha, target + "/audit")) == audit_before


def confirm(client, scope, proposal, key, expected=200, confirmed=True):
    response = call(client, scope + "/proposals/" + proposal["proposal_id"] + "/confirm",
                    method="POST", body={"confirmed": confirmed}, expected=expected,
                    headers={"Idempotency-Key": key})
    return json.loads(response) if expected == 200 else None


key = str(uuid4())
confirm(beta, target, proposal, key, expected=403)
confirm(alpha, other, proposal, key, expected=404)
confirm(alpha, target, proposal, key, expected=400, confirmed=False)
assert json.loads(call(alpha, target)) == before
with ThreadPoolExecutor(max_workers=2) as pool:
    first = pool.submit(confirm, alpha, target, proposal, key)
    retry = pool.submit(confirm, alpha, target, proposal, key)
    receipt = first.result()
    assert retry.result() == receipt
assert receipt["actor"] == "alpha" and receipt["before"] == before
assert receipt["rule_version"] == review["rule_version"]
assert receipt["after"]["revision"] == before["revision"] + 1
assert all(receipt["after"][field] == value for field, value in changes.items())
assert confirm(alpha, target, proposal, key) == receipt
confirm(alpha, target, stale, key, expected=409)
confirm(alpha, target, stale, str(uuid4()), expected=409)
confirm(alpha, target, proposal, str(uuid4()), expected=409)
other_proposal = json.loads(call(alpha, other + "/proposals", method="POST", body={"note": "回滚验证"}))
confirm(alpha, other, other_proposal, key, expected=409)
assert json.loads(call(alpha, target)) == receipt["after"]
assert json.loads(call(alpha, target + "/review", method="POST"))["missing"] == []
history = json.loads(call(alpha, target + "/audit"))
assert len(history) == len(audit_before) + 1 and history[0] == receipt
assert json.loads(call(alpha, other)) == other_before
assert json.loads(call(alpha, other + "/audit")) == other_audit_before
assert json.loads(call(beta, target)) == beta_before
assert json.loads(call(beta, target + "/audit")) == []
for client in clients.values():
    call(client, "/api/session", method="DELETE", expected=204)
    call(client, "/api/enterprises", expected=401)
print("PASS: P1 login and SSE, P2 review, confirmation, idempotency, stale version, RBAC, audit and isolation")
print(f"Verified invoice revision {before['revision']} -> {receipt['after']['revision']}, one audit receipt")
