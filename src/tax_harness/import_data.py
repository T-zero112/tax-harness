"""Import and reconcile the official v1.1.0 material bundle."""

import argparse
import json
import re
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal
from io import BytesIO
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from openpyxl import load_workbook

from .database import Database
from .money import to_cents

ENTERPRISES = ("C001", "C002")
MONTHS = ("2026-08", "2026-09")
INVOICE_HEADER = ("企业编号", "发票编号", "开票日期", "销售方名称", "项目名称（票面）", "价税合计（元）", "用途", "备注")
BANK_HEADER = ("企业编号", "流水编号", "交易日期", "收支方向", "金额（元）", "对方名称", "备注")
RULE_SCOPES = {
    ("R001", "1.0"): (ENTERPRISES, "2026-01-01", 1),
    ("R002", "1.0"): (("C001",), "2026-08-01", 0),
    ("R002", "2.0"): (("C001",), "2026-09-01", 1),
    ("R003", "1.0"): (("C002",), "2026-08-01", 1),
    ("R004", "1.0"): (ENTERPRISES, "2026-01-01", 1),
}


def read_member(bundle: ZipFile, filename: str) -> bytes:
    matches = [name for name in bundle.namelist() if PurePosixPath(name).name == filename]
    if len(matches) != 1:
        raise ValueError(f"材料包中需要且只能有一个 {filename}")
    return bundle.read(matches[0])


def detail_rows(sheet, header):
    rows = iter(sheet.values)
    for row in rows:
        if row[:len(header)] == header:
            break
    else:
        raise ValueError(f"{sheet.title} 缺少预期表头")
    for row in rows:
        if not any(cell is not None for cell in row):
            return
        if row[0] not in ENTERPRISES:
            raise ValueError(f"{sheet.title} 含未知企业 {row[0]}")
        yield row[:len(header)]


def iso_day(value):
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raise ValueError(f"预期 Excel 日期单元格，实际为 {value!r}")


def read_workbook(data):
    workbook = load_workbook(BytesIO(data), read_only=True, data_only=True)
    try:
        invoice_rows = [
            (enterprise, number, iso_day(day), seller, item, to_cents(str(amount)), purpose, note)
            for enterprise, number, day, seller, item, amount, purpose, note in detail_rows(workbook["发票"], INVOICE_HEADER)
        ]
        directions = {"收入": "income", "支出": "expense"}
        bank_rows = [
            (enterprise, number, iso_day(day), directions[direction], to_cents(str(amount)), counterparty, note)
            for enterprise, number, day, direction, amount, counterparty, note in detail_rows(workbook["银行流水"], BANK_HEADER)
        ]
        permissions = {row[0]: tuple(row[2:4]) for row in workbook["企业与用户"].values if row[0] in ("U001", "U002")}
        expected_permissions = {"U001": ("可查看并修改", "可查看并修改"), "U002": ("仅查看", "无权访问")}
        if permissions != expected_permissions:
            raise ValueError("材料包权限表与 v1.1.0 固定权限不一致")
        expected = {
            (row[0], iso_day(row[1])[:7]): (
                row[2], row[3], *(to_cents(Decimal(str(value)).quantize(Decimal("0.01"))) for value in row[4:7])
            )
            for row in workbook["说明"].values if row[0] in ENTERPRISES and isinstance(row[1], date)
        }
        return invoice_rows, bank_rows, expected
    finally:
        workbook.close()


def read_rules(data):
    with ZipFile(BytesIO(data)) as document:
        root = ET.fromstring(document.read("word/document.xml"))
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    sections = {}
    heading = re.compile(r"(R00[1-4]) (.+?) (\d+\.\d+)版(?: 已停用)?$")
    current = None
    for paragraph in root.iter(ns + "p"):
        content = "".join(node.text or "" for node in paragraph.iter(ns + "t")).strip()
        match = heading.fullmatch(content)
        if match:
            doc_id, name, version = match.groups()
            current = (doc_id, version)
            if current in sections:
                raise ValueError(f"重复规则正文 {current}")
            sections[current] = (name, [])
        elif current and content:
            sections[current][1].append(content)
    if sections.keys() != RULE_SCOPES.keys() or any(not lines for _, lines in sections.values()):
        raise ValueError("规则正文与 v1.1.0 版本清单不一致")
    rows = []
    for (doc_id, version), (name, lines) in sections.items():
        enterprises, effective_from, active = RULE_SCOPES[doc_id, version]
        for enterprise in enterprises:
            rows.append((doc_id, version, enterprise, "main", name, effective_from, "\n".join(lines), active))
    return rows


def reconcile(invoices, bank_rows, expected):
    totals = defaultdict(lambda: [0, 0, 0, 0, 0])
    direction_counts = defaultdict(lambda: {"expense": 0, "income": 0})
    for enterprise, _, day, _, _, amount, _, _ in invoices:
        row = totals[enterprise, day[:7]]
        row[0] += 1
        row[2] += amount
    for enterprise, _, day, direction, amount, _, _ in bank_rows:
        key = (enterprise, day[:7])
        totals[key][1] += 1
        totals[key][3 if direction == "expense" else 4] += amount
        direction_counts[key][direction] += 1
    scopes = {(enterprise, month) for enterprise in ENTERPRISES for month in MONTHS}
    if set(totals) != scopes:
        raise ValueError("明细的企业或月份范围与 v1.1.0 不一致")
    for key, values in totals.items():
        if values[:2] != [10, 12] or direction_counts[key] != {"expense": 8, "income": 4}:
            raise ValueError(f"{key} 的明细数量不匹配")
        if tuple(values) != expected.get(key):
            raise ValueError(f"{key} 的明细汇总与说明页核对值不一致")
    return totals


def import_bundle(database: Database, bundle_path: Path) -> dict:
    with ZipFile(bundle_path) as bundle:
        invoices, bank_rows, expected = read_workbook(read_member(bundle, "02_示例数据.xlsx"))
        rules = read_rules(read_member(bundle, "03_业务规则.docx"))
    totals = reconcile(invoices, bank_rows, expected)
    database.initialize()
    with database.write() as connection:
        for table in ("invoices", "bank_transactions", "rules"):
            if connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
                raise ValueError("目标库已有业务数据或规则，请指定新的数据库文件")
        connection.executemany(
            "INSERT INTO invoices(enterprise_id,invoice_id,invoice_date,seller,item_name,amount_cents,purpose,note) VALUES (?,?,?,?,?,?,?,?)", invoices,
        )
        connection.executemany(
            "INSERT INTO bank_transactions(enterprise_id,transaction_id,transaction_date,direction,amount_cents,counterparty,note) VALUES (?,?,?,?,?,?,?)", bank_rows,
        )
        connection.executemany(
            "INSERT INTO rules(doc_id,version,enterprise_id,entry,name,effective_from,content,active) VALUES (?,?,?,?,?,?,?,?)", rules,
        )
    keys = ("invoice_count", "bank_count", "invoice_cents", "expense_cents", "income_cents")
    return {
        "material_version": "1.1.0", "invoices": len(invoices), "bank_transactions": len(bank_rows),
        "rule_versions": len(RULE_SCOPES), "rule_scope_rows": len(rules),
        "scopes": [{"enterprise_id": key[0], "month": key[1], **dict(zip(keys, values, strict=True))}
                   for key, values in sorted(totals.items())],
    }


def main():
    parser = argparse.ArgumentParser(description="导入财税 Harness v1.1.0 材料包")
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--db", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = import_bundle(Database(args.db), args.bundle)
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(1, f"导入失败：{exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
