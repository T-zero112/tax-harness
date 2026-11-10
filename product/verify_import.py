"""P4 end-to-end verification against the local Compose demo."""
import hashlib
import http.cookiejar
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener
from uuid import uuid4

from openpyxl import Workbook


ROOT = Path(__file__).resolve().parent
BASE = os.getenv("PRODUCT_TEST_BASE", "http://127.0.0.1:3100")
IMPORTS = "/api/enterprises/C001/imports"
INVOICES = "/api/enterprises/C001/invoices/"
CONFIG = dict(line.split("=", 1) for line in (ROOT / ".env").read_text().splitlines() if "=" in line)
COMPOSE = ["docker", "compose", "--project-directory", str(ROOT)]


def call(client, path, method="GET", body=None, expected=200, headers=None):
    if isinstance(body, dict):
        body = json.dumps(body).encode()
    request = Request(BASE + path, method=method, data=body,
                      headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with client.open(request, timeout=35) as response:
            assert response.status == expected, (path, response.status)
            return json.load(response)
    except HTTPError as error:
        assert error.code == expected, (path, error.code, error.read().decode(errors="replace"))
        return None


def login(user):
    client = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
    call(client, "/api/session", "POST", {"username": user, "password": CONFIG[f"PRODUCT_{user.upper()}_PASSWORD"]})
    return client


def xlsx(rows):
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["invoice_id", "accounting_period", "purpose", "note"])
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def upload(client, data, expected=200):
    boundary = "p4-" + uuid4().hex
    part = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"p4-test.xlsx\"\r\n"
        "Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
    return call(client, IMPORTS, "POST", part, expected, {"Content-Type": f"multipart/form-data; boundary={boundary}"})


def wait_for(client, path, state):
    until = time.monotonic() + 35
    while time.monotonic() < until:
        current = call(client, path)
        if current["state"] != "queued":
            assert current["state"] == state, current
            return current
        time.sleep(0.4)
    raise AssertionError(f"{path} stayed queued")


def confirm(client, path, expected=200, confirmed=True):
    return call(client, path + "/confirm", "POST", {"confirmed": confirmed}, expected)


def verify_original(digest):
    key = f"alpha/C001/{digest}.xlsx"
    code = f"""
import app
import hashlib
from urllib.error import HTTPError
from urllib.request import Request, urlopen

key = {key!r}
request = Request("http://127.0.0.1:8000/objects/" + key, data=b"wrong bytes",
                  headers={{"Authorization": "Bearer " + app.TOKEN}}, method="PUT")
try:
    with urlopen(request, timeout=10):
        raise AssertionError("Wrong hash accepted")
except HTTPError as error:
    assert error.code == 400, error.code
with app.s3().get_object(Bucket=app.BUCKET, Key=key)["Body"] as stream:
    assert hashlib.sha256(stream.read()).hexdigest() == {digest!r}
"""
    subprocess.run(COMPOSE + ["exec", "-T", "importer", "python", "-"], input=code, text=True, check=True)


def snapshot(job_id, invoice_id):
    alpha = login("alpha")
    job = call(alpha, IMPORTS + "/" + job_id)
    assert job["state"] == "imported"
    verify_original(job["file_sha256"])
    invoice = call(alpha, INVOICES + invoice_id)
    print(json.dumps({"job": job, "invoice": invoice}, ensure_ascii=True, sort_keys=True))


def main():
    alpha, beta = login("alpha"), login("beta")
    invoice_id = "P4-" + uuid4().hex[:12]
    data = xlsx([(invoice_id, "2026-09", None, "P4 合成测试")])
    digest = hashlib.sha256(data).hexdigest()

    subprocess.run(COMPOSE + ["stop", "worker"], check=True)
    try:
        job = upload(alpha, data)
        path = IMPORTS + "/" + job["job_id"]
        assert job["file_sha256"] == digest and job["state"] == "queued"
        assert upload(alpha, data)["job_id"] == job["job_id"]
        assert call(alpha, path + "/retry", "POST")["state"] == "queued"
        call(alpha, INVOICES + "F001")
        assert call(alpha, "/api/enterprises/C001/rules/search?period=2026-09&query=invoice")
        call(alpha, INVOICES + invoice_id, expected=404)
        confirm(alpha, path, expected=409)
        call(beta, path, expected=404)
        call(alpha, path.replace("C001", "C002"), expected=404)
        assert all(item["job_id"] != job["job_id"] for item in call(beta, IMPORTS))
        upload(beta, data, expected=403)
    finally:
        subprocess.run(COMPOSE + ["start", "worker"], check=True)

    preview = wait_for(alpha, path, "ready")
    assert preview["preview"]["errors"] == []
    assert [row["invoice_id"] for row in preview["preview"]["rows"]] == [invoice_id]
    call(alpha, INVOICES + invoice_id, expected=404)
    confirm(alpha, path, expected=400, confirmed=False)
    confirm(beta, path, expected=403)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(confirm, alpha, path)
        second = pool.submit(confirm, alpha, path)
        receipt = first.result()
        assert second.result() == receipt
    assert receipt["state"] == "imported" and receipt["imported_count"] == 1
    assert upload(alpha, data) == receipt
    invoice_path = INVOICES + invoice_id
    saved = call(alpha, invoice_path)
    assert saved["revision"] == 1 and saved["accounting_period"] == "2026-09-01"
    call(beta, invoice_path, expected=404)
    review = call(alpha, invoice_path + "/review", "POST")
    assert review["missing"] == ["purpose"] and review["rule_ref"]["clause"]
    proposal = call(alpha, invoice_path + "/proposals", "POST", {"purpose": "测试采购用途"})
    assert proposal["rule_ref"] == review["rule_ref"]
    completed = call(alpha, invoice_path + "/proposals/" + proposal["proposal_id"] + "/confirm", "POST",
                     {"confirmed": True}, headers={"Idempotency-Key": str(uuid4())})
    assert call(alpha, invoice_path) == completed["after"]
    assert call(alpha, invoice_path + "/review", "POST")["missing"] == []

    bad_id = "BAD-" + uuid4().hex[:12]
    bad = upload(alpha, xlsx([(bad_id, "2026-13", None, None), ("FORMULA", "2026-09", "=1+1", None)]))
    bad_path = IMPORTS + "/" + bad["job_id"]
    errors = wait_for(alpha, bad_path, "invalid")["preview"]["errors"]
    assert [error["row"] for error in errors] == [2, 3]
    confirm(alpha, bad_path, expected=409)
    call(alpha, INVOICES + bad_id, expected=404)

    rollback_id = "A-" + uuid4().hex[:12]
    conflict = upload(alpha, xlsx([(rollback_id, "2026-09", None, None), ("F001", "2026-09", None, None)]))
    conflict_path = IMPORTS + "/" + conflict["job_id"]
    wait_for(alpha, conflict_path, "ready")
    confirm(alpha, conflict_path, expected=409)
    call(alpha, INVOICES + rollback_id, expected=404)
    assert call(alpha, conflict_path)["state"] == "ready"

    verify_original(digest)
    assert call(login("alpha"), path) == receipt
    print("PASS: original object, async queue, retry, row errors, tenant scope, idempotency, atomic rollback, P2/P3 review")
    print(f"Import job {job['job_id']} and invoice {invoice_id}")


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--snapshot":
        snapshot(sys.argv[2], sys.argv[3])
    else:
        main()
