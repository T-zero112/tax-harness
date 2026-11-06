import pytest
from fastapi.testclient import TestClient

from tax_harness.app import create_app

SCOPE = {"enterprise_id": "C001", "month": "2026-09"}
WRITER = {"X-Demo-User": "U001"}
READER = {"X-Demo-User": "U002"}


@pytest.fixture
def workbench(tmp_path):
    app = create_app(tmp_path / "reads.sqlite3")
    with TestClient(app) as client:
        with app.state.database.write() as connection:
            connection.executemany(
                "INSERT INTO invoices(enterprise_id,invoice_id,invoice_date,seller,item_name,amount_cents) "
                "VALUES (?,?,?,?,?,?)",
                [("C001", "F001", "2026-09-01", "甲供方", "材料", 10001),
                 ("C001", "F002", "2026-09-30", "乙供方", "服务", 20002),
                 ("C001", "F003", "2026-08-31", "甲供方", "材料", 30003),
                 ("C002", "F001", "2026-09-01", "乙企供方", "材料", 40004)],
            )
            connection.executemany(
                "INSERT INTO bank_transactions(enterprise_id,transaction_id,transaction_date,direction,amount_cents,counterparty) "
                "VALUES (?,?,?,?,?,?)",
                [("C001", "T001", "2026-09-01", "income", 40001, "对方一"),
                 ("C001", "T002", "2026-09-30", "expense", 10002, "对方二"),
                 ("C001", "T003", "2026-10-01", "income", 90003, "对方三"),
                 ("C002", "T001", "2026-09-01", "expense", 50004, "对方四")],
            )
        yield app, client


def test_invoices_scope_and_filtered_sum(workbench):
    _, client = workbench
    response = client.get("/api/invoices", params=SCOPE, headers=READER)
    assert response.status_code == 200
    data = response.json()["data"]
    assert [record["invoice_id"] for record in data["records"]] == ["F001", "F002"]
    assert data["invoice_count"] == 2
    assert data["invoice_cents"] == 30003

    selected = client.get("/api/invoices", params={**SCOPE, "seller": "甲", "item_name": "材料"}, headers=WRITER).json()
    assert selected["data"]["invoice_cents"] == 10001
    assert selected["data"]["invoice_count"] == 1


def test_bank_directions_are_separate_and_filterable(workbench):
    _, client = workbench
    data = client.get("/api/bank-transactions", params=SCOPE, headers=READER).json()["data"]
    assert [record["transaction_id"] for record in data["records"]] == ["T001", "T002"]
    assert (data["bank_count"], data["income_cents"], data["expense_cents"]) == (2, 40001, 10002)
    expense = client.get("/api/bank-transactions", params={**SCOPE, "direction": "expense"}, headers=WRITER).json()["data"]
    assert (expense["bank_count"], expense["income_cents"], expense["expense_cents"]) == (1, 0, 10002)


def test_record_is_bound_to_enterprise_and_month(workbench):
    _, client = workbench
    path = "/api/invoices/F001"
    alpha = client.get(path, params=SCOPE, headers=WRITER).json()
    beta = client.get(path, params={**SCOPE, "enterprise_id": "C002"}, headers=WRITER).json()
    assert alpha["object_id"] == beta["object_id"] == "F001"
    assert (alpha["data"]["enterprise_id"], alpha["data"]["amount_cents"]) == ("C001", 10001)
    assert (beta["data"]["enterprise_id"], beta["data"]["amount_cents"]) == ("C002", 40004)
    missing = client.get(path, params={**SCOPE, "month": "2026-08"}, headers=WRITER)
    assert missing.status_code == 404
    assert missing.json()["code"] == "not_found"


@pytest.mark.parametrize("path", ["/api/invoices", "/api/bank-transactions", "/api/invoices/F001"])
def test_no_data_leaks_to_an_unauthorized_enterprise(workbench, path):
    _, client = workbench
    denied = client.get(path, params={**SCOPE, "enterprise_id": "C002"}, headers=READER)
    assert denied.status_code == 403
    assert denied.json()["code"] == "forbidden"
    assert "data" not in denied.json()


def test_empty_scope_and_failed_query_are_distinct(workbench):
    app, client = workbench
    empty_scope = {**SCOPE, "month": "2026-07"}
    invoices = client.get("/api/invoices", params=empty_scope, headers=WRITER).json()
    bank = client.get("/api/bank-transactions", params=empty_scope, headers=WRITER).json()
    assert invoices["status"] == "succeeded"
    assert invoices["data"] == {"records": [], "invoice_count": 0, "invoice_cents": 0}
    assert bank["status"] == "succeeded"
    assert bank["data"] == {"records": [], "bank_count": 0, "income_cents": 0, "expense_cents": 0}
    assert client.get("/api/invoices", params={"enterprise_id": "C001"}, headers=WRITER).status_code == 422
    with app.state.database.write() as connection:
        connection.execute("ALTER TABLE invoices RENAME TO unavailable_invoices")
    failed = client.get("/api/invoices", params=SCOPE, headers=WRITER)
    assert failed.status_code == 500
    assert failed.json()["code"] == "tool_failed"
    assert "data" not in failed.json()
