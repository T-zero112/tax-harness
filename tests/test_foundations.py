import sqlite3
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from tax_harness.access import authorize
from tax_harness.app import create_app
from tax_harness.contracts import InvoicePatch, MessageRequest, Scope, ToolResult
from tax_harness.database import Database
from tax_harness.money import to_cents


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "test.sqlite3")
    database.initialize()
    return database


def add_task(connection, task_id="t1", enterprise="C001", month="2026-09", status="waiting_user"):
    connection.execute(
        "INSERT INTO tasks(task_id,user_id,enterprise_id,month,status) VALUES (?,'U001',?,?,?)",
        (task_id, enterprise, month, status),
    )


def add_invoice(connection, enterprise="C001"):
    connection.execute(
        "INSERT INTO invoices(enterprise_id,invoice_id,invoice_date,seller,item_name,amount_cents) "
        "VALUES (?,'F001','2026-09-01','seller','item',100000)", (enterprise,),
    )


def test_http_startup_and_contracts(tmp_path):
    with TestClient(create_app(tmp_path / "http.sqlite3")) as client:
        assert client.get("/health").json()["schema_version"] == 1
        assert "MessageRequest" in client.get("/api/contracts").json()["schemas"]
        assert client.get("/openapi.json").status_code == 200
        params = {"enterprise_id": "C001", "month": "2026-09"}
        assert client.get("/api/scope", params=params).status_code == 422
        assert client.get("/api/scope", params=params, headers={"X-Demo-User": "U002"}).status_code == 200
        params["enterprise_id"] = "C002"
        response = client.get("/api/scope", params=params, headers={"X-Demo-User": "U002"})
        assert response.status_code == 403
        assert response.json()["code"] == "forbidden"


@pytest.mark.parametrize("user,enterprise,write,allowed", [
    ("U001", "C001", True, True), ("U001", "C002", True, True),
    ("U002", "C001", False, True), ("U002", "C001", True, False),
    ("U002", "C002", False, False), ("unknown", "C001", False, False),
])
def test_permissions(db, user, enterprise, write, allowed):
    scope = Scope(user_id=user, enterprise_id=enterprise, month="2026-09")
    with db.connect() as connection:
        if allowed:
            authorize(connection, scope, write=write)
        else:
            with pytest.raises(PermissionError):
                authorize(connection, scope, write=write)


def test_one_unfinished_across_months_and_restart(db):
    with db.write() as connection:
        add_task(connection)
    db.initialize()
    with pytest.raises(sqlite3.IntegrityError), db.write() as connection:
        add_task(connection, "t2", month="2026-08")
    with db.write() as connection:
        add_task(connection, "other", enterprise="C002")
        connection.execute("UPDATE tasks SET status='completed' WHERE task_id='t1'")
        add_task(connection, "t2", month="2026-08")
    with pytest.raises(sqlite3.IntegrityError), db.write() as connection:
        connection.execute("UPDATE tasks SET month='2026-07' WHERE task_id='t2'")


def test_composite_keys_and_atomic_rollback(db):
    with db.write() as connection:
        add_invoice(connection)
        add_invoice(connection, "C002")
    with pytest.raises(RuntimeError), db.write() as connection:
        connection.execute("UPDATE invoices SET purpose='office' WHERE enterprise_id='C001'")
        raise RuntimeError("injected failure")
    with db.connect() as connection:
        assert [r[0] for r in connection.execute("SELECT purpose FROM invoices")] == [None, None]
    with pytest.raises(sqlite3.IntegrityError), db.write() as connection:
        add_invoice(connection)
    with pytest.raises(sqlite3.IntegrityError), db.write() as connection:
        connection.execute("UPDATE invoices SET amount_cents=1 WHERE enterprise_id='C001'")


def test_unknown_write_blocks_other_operation_and_task_close(db):
    with db.write() as connection:
        add_task(connection)
        add_invoice(connection)
        connection.execute("INSERT INTO requests VALUES ('U001','r1','s1','{}','t1','running',NULL)")
        connection.execute(
            "INSERT INTO operations(operation_id,request_id,task_id,user_id,enterprise_id,month,object_id,patch_json,status) "
            "VALUES ('o1','r1','t1','U001','C001','2026-09','F001','{}','unknown')"
        )
    with pytest.raises(sqlite3.IntegrityError), db.write() as connection:
        connection.execute("UPDATE tasks SET status='cancelled' WHERE task_id='t1'")
    with pytest.raises(sqlite3.IntegrityError), db.write() as connection:
        connection.execute(
            "INSERT INTO operations(operation_id,request_id,task_id,user_id,enterprise_id,month,object_id,patch_json,status) "
            "VALUES ('o2','r1','t1','U001','C001','2026-09','F001','{}','prepared')"
        )
    with db.write() as connection:
        connection.execute("UPDATE operations SET status='confirmed' WHERE operation_id='o1'")
        connection.execute("UPDATE tasks SET status='completed' WHERE task_id='t1'")


def test_contract_boundaries():
    request = MessageRequest(request_id="r1", session_id="s1", enterprise_id="C001", text="查发票")
    assert request.month is None
    with pytest.raises(ValidationError):
        MessageRequest(**request.model_dump(), user_id="U001")
    with pytest.raises(ValidationError):
        Scope(user_id="U001", enterprise_id="C001", month="2026-13")
    with pytest.raises(ValidationError):
        InvoicePatch(amount_cents=1)
    with pytest.raises(ValidationError):
        InvoicePatch()
    assert InvoicePatch(purpose=None).model_dump(exclude_unset=True) == {"purpose": None}
    with pytest.raises(ValidationError):
        ToolResult(status="failed")


@pytest.mark.parametrize("value,expected", [("460.50", 46050), (Decimal("1000"), 100000), ("0", 0)])
def test_exact_money(value, expected):
    assert to_cents(value) == expected


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", "1.001", "bad", 0.1])
def test_invalid_money(value):
    with pytest.raises(ValueError):
        to_cents(value)
