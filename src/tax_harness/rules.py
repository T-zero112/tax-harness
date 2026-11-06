import calendar
import json
import re
import sqlite3
from datetime import date

from .access import authorize
from .contracts import Citation, MutationReceipt, RevisionRef, RuleHit, Scope
from .database import Database
from .ports import RevisionConflict


def rule_identifier(hit: RuleHit) -> str:
    citation = hit.citation
    return json.dumps([citation.doc_id, citation.version, hit.enterprise_id, citation.entry],
                      ensure_ascii=False, separators=(",", ":"))


def as_hit(row) -> RuleHit:
    citation = Citation(**{field: row[field] for field in ("doc_id", "name", "version", "entry")})
    return RuleHit(citation=citation, enterprise_id=row["enterprise_id"], effective_from=row["effective_from"],
                   content=row["content"], revision=row["revision"])


def terms(query):
    tokens = re.findall(r"[a-z0-9.]+", query.lower())
    for run in re.findall(r"[\u3400-\u9fff]+", query):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[index:index + 2] for index in range(len(run) - 1))
    return set(tokens)


class SqliteRuleStore:
    def __init__(self, database: Database):
        self.database = database

    def list_versions(self, scope: Scope):
        with self.database.connect() as connection:
            authorize(connection, scope)
            rows = connection.execute(
                "SELECT * FROM rules WHERE enterprise_id=? ORDER BY doc_id,effective_from DESC,version,entry",
                (scope.enterprise_id,),
            )
            return [{"rule": as_hit(row), "active": bool(row["active"])} for row in rows]

    def _applicable(self, scope):
        year, month = map(int, scope.month.split("-"))
        cutoff = date(year, month, calendar.monthrange(year, month)[1]).isoformat()
        with self.database.connect() as connection:
            authorize(connection, scope)
            rows = connection.execute(
                "SELECT r.* FROM rules r WHERE r.enterprise_id=? AND r.active=1 AND r.effective_from<=? "
                "AND NOT EXISTS (SELECT 1 FROM rules n WHERE n.doc_id=r.doc_id AND n.enterprise_id=r.enterprise_id "
                "AND n.active=1 AND n.effective_from>r.effective_from AND n.effective_from<=?) "
                "ORDER BY r.doc_id,r.version,r.entry", (scope.enterprise_id, cutoff, cutoff),
            )
            return [as_hit(row) for row in rows]

    def search(self, scope: Scope, query: str) -> list[RuleHit]:
        candidates = self._applicable(scope)
        if not query.strip():
            return candidates
        keywords = terms(query)
        ranked = []
        for hit in candidates:
            label = f"{hit.citation.doc_id} {hit.citation.name} {hit.citation.version}".lower()
            body = hit.content.lower()
            score = sum(3 if word in label else int(word in body) for word in keywords)
            if score:
                ranked.append((score, hit))
        return [hit for _, hit in sorted(ranked, key=lambda pair: -pair[0])]

    def add_version(self, scope: Scope, rule: RuleHit) -> MutationReceipt:
        effective_from = date.fromisoformat(rule.effective_from).isoformat()
        if rule.enterprise_id != scope.enterprise_id:
            raise PermissionError("规则企业与当前范围不一致")
        if not rule.content.strip() or not rule.citation.entry.strip() or not rule.citation.name.strip():
            raise ValueError("规则名称、条目和正文不能为空")
        citation = rule.citation
        reference = RevisionRef(kind="rule", identifier=rule_identifier(rule), revision=1)
        with self.database.write() as connection:
            authorize(connection, scope, write=True)
            existing = connection.execute(
                "SELECT DISTINCT name,effective_from,active FROM rules WHERE enterprise_id=? AND doc_id=? AND version=?",
                (scope.enterprise_id, citation.doc_id, citation.version),
            ).fetchall()
            if any(tuple(row) != (citation.name, effective_from, 1) for row in existing):
                raise RevisionConflict("现有版本的名称、生效日或状态不同")
            competing = connection.execute(
                "SELECT 1 FROM rules WHERE enterprise_id=? AND doc_id=? AND active=1 AND effective_from=? AND version<>?",
                (scope.enterprise_id, citation.doc_id, effective_from, citation.version),
            ).fetchone()
            if competing:
                raise RevisionConflict("同一规则已有相同生效日的启用版本")
            try:
                connection.execute(
                    "INSERT INTO rules(doc_id,version,enterprise_id,entry,name,effective_from,content,active) "
                    "VALUES (?,?,?,?,?,?,?,1)", (citation.doc_id, citation.version, scope.enterprise_id, citation.entry,
                                                citation.name, effective_from, rule.content.strip()),
                )
            except sqlite3.IntegrityError as exc:
                raise RevisionConflict("规则条目已存在") from exc
        return MutationReceipt(reference=reference)

    def deactivate(self, scope: Scope, doc_id: str, version: str, expected_revision: int) -> MutationReceipt:
        with self.database.write() as connection:
            authorize(connection, scope, write=True)
            key = (scope.enterprise_id, doc_id, version)
            rows = connection.execute(
                "SELECT * FROM rules WHERE enterprise_id=? AND doc_id=? AND version=? AND active=1 ORDER BY entry", key,
            ).fetchall()
            if not rows:
                raise KeyError("找不到启用的规则版本")
            if any(row["revision"] != expected_revision for row in rows):
                raise RevisionConflict("规则版本已更新，请重新读取")
            reference = RevisionRef(kind="rule", identifier=rule_identifier(as_hit(rows[0])), revision=expected_revision + 1)
            connection.execute("UPDATE rules SET active=0,revision=revision+1 WHERE enterprise_id=? AND doc_id=? AND version=?", key)
        return MutationReceipt(reference=reference)

    def verify_citation(self, scope: Scope, citation: Citation) -> bool:
        return any(hit.citation == citation for hit in self._applicable(scope))

    def is_current(self, scope: Scope, reference: RevisionRef) -> bool:
        return reference.kind == "rule" and any(
            rule_identifier(hit) == reference.identifier and hit.revision == reference.revision
            for hit in self._applicable(scope)
        )
