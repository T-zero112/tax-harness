from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from tax_harness.app import create_app

SCOPE = {"enterprise_id": "C001", "month": "2026-09"}
WRITER = {"X-Demo-User": "U001"}
READER = {"X-Demo-User": "U002"}
INVOICE_PATH = "/api/invoices/F001"


def save_request(**changes):
    return {"task_id": "t1", "session_id": "s1", "request_id": "r1", "operation_id": "o1",
            "expected_revision": 1, "patch": {"purpose": "办公"}, **changes}


@pytest.fixture
def service(tmp_path):
    app = create_app(tmp_path / "writes.sqlite3")
    with TestClient(app) as client:
        with app.state.database.write() as connection:
            connection.executemany(
                "INSERT INTO invoices(enterprise_id,invoice_id,invoice_date,seller,item_name,amount_cents,purpose,note) "
                "VALUES (?,'F001','2026-09-01','供方','项目',?,NULL,?)",
                [("C001", 50000, "原备注"), ("C002", 60000, "乙企业备注")],
            )
        yield client, app.state.database


def save(client, body=None, scope=SCOPE, headers=WRITER):
    return client.patch(INVOICE_PATH, params=scope, headers=headers, json=body or save_request())


def read(client, scope=SCOPE):
    return client.get(INVOICE_PATH, params=scope, headers=WRITER).json()["data"]


def close(client, status="completed"):
    return client.patch("/api/tasks/t1/status", params=SCOPE, headers=WRITER,
                        json={"status": status, "expected_revision": 1})


def test_save_readback_and_replay_survive_a_new_app(service):
    client, database = service
    request = save_request(patch={"purpose": "办公", "note": "复核完成"})
    response = save(client, request)
    assert response.status_code == 200
    result = response.json()
    data = result["data"]
    assert result["status"] == "succeeded"
    assert data["operation_status"] == "confirmed"
    assert data["record"] == read(client)
    assert (data["record"]["purpose"], data["record"]["note"], data["record"]["revision"]) == ("办公", "复核完成", 2)
    assert data["verification"]["before"] == {"purpose": None, "note": "原备注"}
    assert read(client, {**SCOPE, "enterprise_id": "C002"})["purpose"] is None

    with TestClient(create_app(database.path)) as restarted:
        assert save(restarted, request).json() == result
        assert restarted.get("/api/operations/o1", params=SCOPE, headers=WRITER).json() == result
        assert restarted.get("/api/operations/o1", params=SCOPE, headers=READER).status_code == 404
        assert close(restarted).json()["status"] == "completed"
        assert save(restarted, request).json() == result
    with database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM operations").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM events WHERE kind='invoice_saved'").fetchone()[0] == 1


def test_revision_and_idempotency_conflicts_and_explicit_clear(service):
    client, _ = service
    assert save(client).status_code == 200
    assert save(client, save_request(patch={"purpose": "差旅"})).status_code == 409
    assert save(client, save_request(session_id="s2")).status_code == 409
    assert save(client, save_request(request_id="r2")).status_code == 409
    assert save(client, save_request(request_id="r2", operation_id="o2")).status_code == 409
    no_change = save(client, save_request(request_id="r2", operation_id="o2", expected_revision=2)).json()
    assert no_change["data"]["verification"]["changed"] is False
    assert no_change["data"]["record"]["revision"] == 2
    clear = save(client, save_request(request_id="r3", operation_id="o3", expected_revision=2, patch={"note": None}))
    assert clear.status_code == 200
    assert clear.json()["data"]["record"]["note"] is None
    assert read(client)["purpose"] == "办公"
    assert read(client)["revision"] == 3


def test_scope_permission_and_fields_are_checked_before_write(service):
    client, database = service
    assert save(client, headers=READER).status_code == 403
    assert save(client, scope={**SCOPE, "enterprise_id": "C002"}, headers=READER).status_code == 403
    assert save(client, scope={**SCOPE, "month": "2026-08"}).status_code == 404
    assert save(client, save_request(patch={"amount_cents": 1})).status_code == 422
    assert save(client, save_request(patch={})).status_code == 422
    with database.write() as connection:
        connection.execute("INSERT INTO tasks(task_id,user_id,enterprise_id,month,status) VALUES ('t1','U001','C002','2026-09','running')")
    assert save(client).status_code == 404
    assert read(client)["revision"] == 1
    with database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM operations").fetchone()[0] == 0


def test_duplicate_requests_from_two_threads_save_once(service):
    client, database = service
    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = list(executor.map(lambda _: save(client), range(2)))
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert read(client)["revision"] == 2
    with database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM operations").fetchone()[0] == 1


@pytest.mark.parametrize("trigger", [
    "CREATE TRIGGER fail_event BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT, 'forced failure'); END",
    "CREATE TRIGGER wrong_value AFTER UPDATE OF purpose ON invoices "
    "BEGIN UPDATE invoices SET note='unexpected' WHERE enterprise_id=NEW.enterprise_id AND invoice_id=NEW.invoice_id; END",
])
def test_write_and_operation_roll_back_after_fault(service, trigger):
    client, database = service
    original = read(client)
    with database.write() as connection:
        connection.execute(trigger)
    failed = save(client)
    assert failed.status_code == 500
    assert failed.json()["code"] == "tool_failed"
    assert read(client) == original
    with database.connect() as connection:
        for table in ("tasks", "requests", "operations", "events"):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


@pytest.mark.parametrize("status,tool_status", [("unknown", "unknown"), ("prepared", "pending"), ("dispatched", "pending")])
def test_unresolved_operation_blocks_writes_and_task_closure(service, status, tool_status):
    client, database = service
    request = save_request()
    assert save(client, request).status_code == 200
    with database.write() as connection:
        connection.execute("UPDATE operations SET status=?,result_json=NULL,verification_json=NULL", (status,))
    operation = client.get("/api/operations/o1", params=SCOPE, headers=WRITER)
    assert operation.status_code == 200
    assert operation.json()["status"] == tool_status
    assert save(client, request).json() == operation.json()
    second = save_request(request_id="r2", operation_id="o2", session_id="s2", expected_revision=2, patch={"purpose": "差旅"})
    assert save(client, second).status_code == 409
    assert close(client).status_code == 409
    assert close(client, "cancelled").status_code == 409
    assert read(client)["purpose"] == "办公"
    with database.connect() as connection:
        assert connection.execute("SELECT status FROM tasks").fetchone()[0] == "running"
        assert connection.execute("SELECT count(*) FROM operations").fetchone()[0] == 1
