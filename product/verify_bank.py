"""Verify product bank queries and PostgreSQL write protection with demo data."""
import http.cookiejar
import subprocess
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, build_opener
from uuid import uuid4

from verify_access import change_role
from verify_import import COMPOSE, call, login


def path(enterprise="C001", period="2026-09", **filters):
    return f"/api/enterprises/{enterprise}/bank-transactions?" + urlencode({"period": period, **filters})


def database_check():
    sql = """
    BEGIN;
    DO $test$
    BEGIN
        BEGIN
            INSERT INTO product_bank_transaction
            SELECT tenant_id, enterprise_id, 'T-TEST', accounting_period, transaction_date,
                   direction, amount_cents, counterparty, note
            FROM product_bank_transaction WHERE tenant_id = 'alpha' AND enterprise_id = 'C001' AND transaction_id = 'T001';
            RAISE EXCEPTION 'INSERT unexpectedly allowed';
        EXCEPTION WHEN read_only_sql_transaction THEN NULL;
        END;
        BEGIN
            UPDATE product_bank_transaction SET amount_cents = 0
            WHERE tenant_id = 'alpha' AND enterprise_id = 'C001' AND transaction_id = 'T001';
            RAISE EXCEPTION 'UPDATE unexpectedly allowed';
        EXCEPTION WHEN read_only_sql_transaction THEN NULL;
        END;
        BEGIN
            DELETE FROM product_bank_transaction
            WHERE tenant_id = 'alpha' AND enterprise_id = 'C001' AND transaction_id = 'T001';
            RAISE EXCEPTION 'DELETE unexpectedly allowed';
        EXCEPTION WHEN read_only_sql_transaction THEN NULL;
        END;
        BEGIN
            TRUNCATE product_bank_transaction;
            RAISE EXCEPTION 'TRUNCATE unexpectedly allowed';
        EXCEPTION WHEN read_only_sql_transaction THEN NULL;
        END;
    END;
    $test$;
    ROLLBACK;
    """
    subprocess.run(COMPOSE + ["exec", "-T", "db", "psql", "-X", "-U", "tax_product",
                              "-d", "tax_product", "-v", "ON_ERROR_STOP=1"],
                   input=sql, text=True, check=True)


def main():
    admin, beta = login("alpha"), login("beta")
    target = path(transaction_id="T001")
    other = path("C002", transaction_id="T001")
    result = call(admin, target)
    different_enterprise = call(admin, other)
    different_tenant = call(beta, target)
    for answer, amount, direction in [(result, "123456", "income"), (different_enterprise, "259900", "expense"),
                                      (different_tenant, "870100", "income")]:
        assert len(answer["records"]) == 1 and not answer["has_more"]
        record = answer["records"][0]
        assert (record["transaction_id"], record["amount_cents"], record["direction"]) == ("T001", amount, direction)
        assert "tenant_id" not in record and "tenant_id" not in answer
    assert [row["transaction_id"] for row in call(admin, path(period="2026-08"))["records"]] == ["T002"]
    assert call(admin, path(period="2026-08", transaction_id="T001"))["records"] == []
    assert call(admin, path(transaction_id="missing"))["records"] == []
    assert call(admin, path(offset=1))["records"] == []
    call(admin, path(period="2026-13"), expected=400)
    call(admin, path(transaction_id="T001' OR 1=1"), expected=400)
    call(admin, path(offset=-1), expected=400)
    call(build_opener(), target, expected=401)
    call(beta, other, expected=404)
    assert call(beta, target, headers={"X-Tenant-Id": "alpha", "X-Role": "admin"}) == different_tenant

    username = "bank-test-" + uuid4().hex[:12]
    credentials = {"username": username, "password": uuid4().hex}
    members = "/api/enterprises/C001/members"
    call(admin, members, "POST", {**credentials, "role": "viewer"})
    try:
        viewer = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
        call(viewer, "/api/session", "POST", credentials)
        assert call(viewer, target) == result
        call(viewer, other, expected=404)
    finally:
        change_role(admin, members + "/" + username)
    call(viewer, target, expected=404)

    for method in ("POST", "PUT", "PATCH", "DELETE"):
        call(admin, target, method, {"amount_cents": "0"}, expected=405)
    database_check()
    assert call(admin, target) == result
    assert call(admin, other) == different_enterprise
    assert call(beta, target) == different_tenant
    print("PASS: period and ID filters, same-number isolation, permissions, HTTP and database read-only")


if __name__ == "__main__":
    main()
