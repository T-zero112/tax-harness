"""Verify live account and enterprise-role enforcement through the product entry point."""
import http.cookiejar
import json
from urllib.request import HTTPCookieProcessor, Request, build_opener
from uuid import uuid4

from verify_import import BASE, call, login


def change_role(client, path, role=None, method=None):
    payload = json.dumps({"role": role}).encode() if role is not None else None
    request = Request(BASE + path, data=payload, method=method or ("PATCH" if role is not None else "DELETE"),
                      headers={"Content-Type": "application/json"})
    with client.open(request, timeout=15) as response:
        assert response.status == 204


def main():
    admin, beta = login("alpha"), login("beta")
    members = "/api/enterprises/C001/members"
    invoice = "/api/enterprises/C001/invoices/F001"
    rules = "/api/enterprises/C001/rules"
    username = "access-test-" + uuid4().hex[:12]
    credentials = {"username": username, "password": uuid4().hex}
    profile = {**credentials, "role": "viewer"}
    call(beta, members, "POST", profile, expected=403)
    call(admin, members, "POST", {**profile, "role": "admin"}, expected=400)
    assert call(admin, members, "POST", profile) == {"username": username, "role": "viewer"}
    member_path = members + "/" + username
    try:
        assert {"username": username, "role": "viewer"} in call(admin, members)
        user = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
        call(user, "/api/session", "POST", {**credentials, "password": "wrong-password"}, expected=401)
        assert call(user, "/api/session", "POST", credentials) == {"tenant_id": "alpha", "username": username}
        assert call(user, "/api/enterprise-access") == [{"enterprise_id": "C001", "role": "viewer"}]
        assert [item["enterprise_id"] for item in call(user, "/api/enterprises")] == ["C001"]
        alpha_before = call(user, invoice)
        beta_before = call(beta, invoice)
        assert alpha_before != beta_before
        assert call(user, invoice, headers={"X-Tenant-Id": "beta", "X-Role": "admin"}) == alpha_before
        call(user, invoice.replace("C001", "C002"), expected=404)
        call(user, invoice + "/proposals", "POST", {"note": "forbidden"}, expected=403,
             headers={"X-Tenant-Id": "beta", "X-Role": "admin"})
        call(user, members, expected=403)
        change_role(admin, member_path, "editor")
        assert call(user, "/api/enterprise-access") == [{"enterprise_id": "C001", "role": "editor"}]
        second = member_path.replace("C001", "C002")
        call(admin, members + "/beta", "PUT", {"role": "viewer"}, expected=404)
        change_role(admin, second, "viewer", "PUT")
        try:
            assert {row["enterprise_id"]: row["role"] for row in call(user, "/api/enterprise-access")} == {
                "C001": "editor", "C002": "viewer"}
            call(user, invoice.replace("C001", "C002"))
            call(user, invoice.replace("C001", "C002") + "/proposals", "POST", {"note": "forbidden"}, expected=403)
        finally:
            change_role(admin, second)
        call(user, invoice.replace("C001", "C002"), expected=404)
        call(user, members, expected=403)
        call(user, rules, "POST", {}, expected=403)
        note = "权限验收 " + username
        proposal = call(user, invoice + "/proposals", "POST", {"note": note})
        path = invoice + "/proposals/" + proposal["proposal_id"] + "/confirm"
        key = {"Idempotency-Key": str(uuid4())}
        change_role(admin, member_path, "viewer")
        call(user, path, "POST", {"confirmed": True}, 403, key)
        assert call(user, invoice) == alpha_before
        change_role(admin, member_path, "editor")
        receipt = call(user, path, "POST", {"confirmed": True}, headers=key)
        assert receipt["actor"] == username and receipt["after"]["note"] == note
        assert receipt["after"]["revision"] == alpha_before["revision"] + 1
        assert call(user, invoice) == receipt["after"]
        assert call(beta, invoice) == beta_before
    finally:
        change_role(admin, member_path)
    assert call(user, "/api/enterprises") == []
    call(user, invoice, expected=404)
    call(user, path, "POST", {"confirmed": True}, 404, key)
    print("PASS: database login, viewer/editor/admin, cross-enterprise and tenant isolation, live revocation, audit identity")


if __name__ == "__main__":
    main()
