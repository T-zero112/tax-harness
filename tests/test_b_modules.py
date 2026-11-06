import sqlite3
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient

from tax_harness.app import create_app
from tax_harness.contracts import Citation, HistoryMessage, RevisionRef, RuleHit, Scope
from tax_harness.database import Database
from tax_harness.import_data import import_bundle
from tax_harness.memory import SqliteMemoryStore
from tax_harness.ports import RevisionConflict
from tax_harness.rules import SqliteRuleStore, rule_identifier


ALPHA = Scope(user_id="U001", enterprise_id="C001", month="2026-09")
BETA = Scope(user_id="U001", enterprise_id="C002", month="2026-09")
READER = Scope(user_id="U002", enterprise_id="C001", month="2026-09")


def seed_messages(database):
    with database.write() as connection:
        connection.executemany(
            "INSERT INTO tasks(task_id,user_id,enterprise_id,month,status) VALUES (?,?,?,'2026-09','completed')",
            [("ta", "U001", "C001"), ("tb", "U001", "C002"), ("tr", "U002", "C001")],
        )
        connection.executemany(
            "INSERT INTO messages(message_id,task_id,session_id,role,content) VALUES (?,?,?,?,?)",
            [("ma", "ta", "s1", "user", "以后汇总先列总额"), ("mb", "tb", "s2", "user", "乙企业偏好"),
             ("mr", "tr", "s3", "user", "我喜欢按日期排序"), ("response", "ta", "s1", "assistant", "收到")],
        )


@pytest.fixture
def database(tmp_path):
    db = Database(tmp_path / "b.sqlite3")
    db.initialize()
    seed_messages(db)
    return db


def test_preference_revisions_hide_the_original_source(database):
    store = SqliteMemoryStore(database)
    memory = store.create(ALPHA, "汇总先列总额", "ma")
    ref = RevisionRef(kind="memory", identifier=memory.memory_id, revision=1)
    assert store.is_current(ALPHA, ref)
    history = [HistoryMessage(message_id=key, role="user", content="untrusted") for key in ("ma", "mb", "mr", "response")]
    assert [(m.message_id, m.content) for m in store.filter_history(ALPHA, history)] == [
        ("ma", "以后汇总先列总额"), ("response", "收到"),
    ]
    receipt = store.correct(ALPHA, memory.memory_id, "汇总按类别分组", 1)
    assert receipt.invalidated_message_ids == ["ma"]
    assert not store.is_current(ALPHA, ref)
    assert store.is_current(ALPHA, receipt.reference)
    assert [m.message_id for m in store.filter_history(ALPHA, history)] == ["response"]
    with pytest.raises(RevisionConflict):
        store.delete(ALPHA, memory.memory_id, 1)
    store.delete(ALPHA, memory.memory_id, 2)
    assert store.list_active(ALPHA) == []
    assert not store.is_current(ALPHA, receipt.reference)
    assert [m.message_id for m in store.filter_history(ALPHA, history)] == ["response"]


def test_preference_scope_and_source(database):
    store = SqliteMemoryStore(database)
    local = store.create(ALPHA, "仅甲企业", "ma")
    wide = store.create(ALPHA, "按日期排序", "ma", user_wide=True)
    assert store.list_active(BETA) == [wide]
    assert store.list_active(READER) == []
    with pytest.raises(KeyError):
        store.create(BETA, "错误来源", "ma")
    with pytest.raises(KeyError):
        store.create(ALPHA, "助手来源", "response")
    with pytest.raises(KeyError):
        store.delete(READER, local.memory_id, 1)
    assert store.create(READER, "按日期排序", "mr").user_id == READER.user_id


def rule(version, start, enterprise="C001"):
    return RuleHit(citation=Citation(doc_id="R002", name="发票用途规则", version=version, entry="main"),
                   enterprise_id=enterprise, effective_from=start, content="发票需要按业务要求填写用途", revision=1)


def test_rule_effective_version_and_invalidation(database):
    store = SqliteRuleStore(database)
    old = rule("1.0", "2026-08-01")
    current = rule("2.0", "2026-09-01")
    old_ref = store.add_version(ALPHA, old).reference
    current_ref = store.add_version(ALPHA, current).reference
    store.add_version(ALPHA, rule("3.0", "2026-10-01"))
    assert store.search(ALPHA, "怎样填写发票用途") == [current]
    assert store.search(BETA, "发票用途") == []
    assert store.verify_citation(READER, current.citation)
    assert not store.verify_citation(ALPHA, old.citation)
    assert not store.is_current(ALPHA, old_ref)
    assert store.is_current(ALPHA, current_ref)
    assert current_ref.identifier == rule_identifier(current)
    assert store.search(ALPHA.model_copy(update={"month": "2026-08"}), "用途") == [old]
    with pytest.raises(PermissionError):
        store.deactivate(READER, "R002", "2.0", 1)
    with pytest.raises(RevisionConflict):
        store.deactivate(ALPHA, "R002", "2.0", 2)
    store.deactivate(ALPHA, "R002", "1.0", 1)
    store.deactivate(ALPHA, "R002", "2.0", 1)
    assert store.search(ALPHA, "用途") == []
    assert not store.verify_citation(ALPHA, current.citation)
    assert not store.is_current(ALPHA, current_ref)


def test_workbench_routes_share_database_and_scope(tmp_path):
    app = create_app(tmp_path / "workbench.sqlite3")
    with TestClient(app) as client:
        seed_messages(app.state.database)
        params = {"enterprise_id": "C001", "month": "2026-09"}
        owner = {"X-Demo-User": "U001"}
        reader = {"X-Demo-User": "U002"}
        assert client.get("/").status_code == 200
        assert client.get("/assets/workbench.js").status_code == 200
        assert [m["message_id"] for m in client.get("/api/workbench/records", params=params, headers=reader).json()["source_messages"]] == ["mr"]
        response = client.post("/api/memories", params=params, headers=owner,
                               json={"content": "先列总额", "source_message_id": "ma"})
        assert response.status_code == 201
        memory_id = response.json()["memory_id"]
        response = client.patch(f"/api/memories/{memory_id}", params=params, headers=owner,
                                json={"content": "先列明细", "expected_revision": 2})
        assert response.status_code == 409
        assert client.get("/api/memories", params=params, headers=reader).json() == []
        rule_body = {"doc_id": "R002", "name": "发票用途", "version": "2.0", "effective_from": "2026-09-01", "content": "填写用途"}
        assert client.post("/api/rules", params=params, headers=reader, json=rule_body).status_code == 403
        assert client.post("/api/rules", params=params, headers=owner, json=rule_body).status_code == 201
        assert client.get("/api/rules", params={**params, "query": "用途"}, headers=owner).json()[0]["citation"]["version"] == "2.0"
        assert client.get("/api/workbench/records", params={**params, "enterprise_id": "C002"}, headers=reader).status_code == 403


def test_failed_import_rolls_back(database, tmp_path, monkeypatch):
    from tax_harness import import_data

    path = tmp_path / "bundle.zip"
    with ZipFile(path, "w") as archive:
        archive.writestr("02_示例数据.xlsx", "placeholder")
        archive.writestr("03_业务规则.docx", "placeholder")
    invoices = [("C001", "F001", "2026-09-01", "供方", "项目", 10000, None, None)]
    monkeypatch.setattr(import_data, "read_workbook", lambda _: (invoices, [], {}))
    monkeypatch.setattr(import_data, "reconcile", lambda *_: {})
    monkeypatch.setattr(import_data, "read_rules", lambda _: [("R001", "1.0", "missing", "main", "规则", "2026-01-01", "正文", 1)])
    with pytest.raises(sqlite3.IntegrityError):
        import_bundle(database, path)
    with database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM invoices").fetchone()[0] == 0
