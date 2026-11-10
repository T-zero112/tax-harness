import hashlib
import io
import json
import os
import re
import secrets
import urllib.request
import zipfile
from xml.etree import ElementTree
from uuid import UUID

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from celery import Celery
from fastapi import FastAPI, Header, HTTPException, Request
from openpyxl import load_workbook
from pydantic import BaseModel

MAX_BYTES = 2 * 1024 * 1024
MAX_ROWS = 1000
HEADERS = ("invoice_id", "accounting_period", "purpose", "note")
BUCKET = "invoice-imports"
TOKEN = os.environ["IMPORT_TOKEN"]
KEY = re.compile(r"[A-Za-z0-9_-]+/[A-Za-z0-9_-]+/[0-9a-f]{64}\.xlsx")
celery = Celery("invoice_import", broker="redis://redis:6379/1")
celery.conf.update(task_acks_late=True, task_reject_on_worker_lost=True,
                   task_time_limit=90, task_ignore_result=True, worker_prefetch_multiplier=1)
app = FastAPI()


def s3():
    return boto3.client("s3", endpoint_url="http://objects:8333",
                        aws_access_key_id=os.environ["S3_USER"],
                        aws_secret_access_key=os.environ["S3_PASSWORD"],
                        config=Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 2},
                                      s3={"addressing_style": "path"}))


def authorize(authorization: str):
    if not secrets.compare_digest(authorization.encode(), ("Bearer " + TOKEN).encode()):
        raise HTTPException(401)


def ensure_bucket(client):
    try:
        client.head_bucket(Bucket=BUCKET)
    except ClientError as error:
        if error.response["ResponseMetadata"]["HTTPStatusCode"] != 404:
            raise
        try:
            client.create_bucket(Bucket=BUCKET)
        except ClientError as conflict:
            if conflict.response["Error"]["Code"] != "BucketAlreadyOwnedByYou":
                raise


class Job(BaseModel):
    job_id: UUID
    attempt: UUID
    object_key: str


@app.get("/health")
def health():
    return {"status": "UP"}


@app.put("/objects/{key:path}")
async def upload(key: str, request: Request, authorization: str = Header(default="")):
    authorize(authorization)
    if not KEY.fullmatch(key):
        raise HTTPException(400)
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > MAX_BYTES:
            raise HTTPException(413)
    if hashlib.sha256(data).hexdigest() != key.rsplit("/", 1)[-1][:-5]:
        raise HTTPException(400)
    # The async route consumes the bounded request body. S3 I/O runs off the event loop.
    from starlette.concurrency import run_in_threadpool
    await run_in_threadpool(store, key, bytes(data))
    return {"stored": True}


def store(key, data):
    client = s3()
    ensure_bucket(client)
    client.put_object(Bucket=BUCKET, Key=key, Body=data,
                      ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.post("/jobs", status_code=202)
def enqueue(job: Job, authorization: str = Header(default="")):
    authorize(authorization)
    if not KEY.fullmatch(job.object_key):
        raise HTTPException(400)
    parse_invoice.delay(str(job.job_id), str(job.attempt), job.object_key)
    return {"queued": True}


def parse_xlsx(raw):
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries = archive.infolist()
        if len(entries) > 100 or sum(entry.file_size for entry in entries) > 24 * 1024 * 1024:
            raise ValueError("工作簿解压后超出限制")
        if any(entry.filename.endswith("vbaProject.bin") for entry in entries):
            raise ValueError("仅接受无宏的 Excel 工作簿")
        for entry in entries:
            if not re.fullmatch(r"xl/worksheets/[^/]+\.xml", entry.filename):
                continue
            with archive.open(entry) as xml:
                for _, element in ElementTree.iterparse(xml, events=("end",)):
                    if element.tag.endswith("}c") and not re.fullmatch(r"[A-D][1-9][0-9]{0,3}", element.get("r", "")):
                        raise ValueError("工作表超出规定列范围")
                    element.clear()
    workbook = load_workbook(io.BytesIO(raw), read_only=True, data_only=False, keep_links=False)
    try:
        if len(workbook.worksheets) != 1:
            raise ValueError("请提供仅含一个工作表的 Excel 文件")
        sheet = workbook.worksheets[0]
        sheet.reset_dimensions()
        records = sheet.iter_rows(max_col=5)
        header = next(records, ())
        if tuple(cell.value for cell in header[:4]) != HEADERS or any(cell.value is not None for cell in header[4:]):
            raise ValueError("表头须为 invoice_id、accounting_period、purpose、note")
        seen = set()
        rows, errors = [], []
        for number, cells in enumerate(records, start=2):
            if number > MAX_ROWS + 1:
                raise ValueError("每次最多导入 1000 行")
            if all(cell.value is None for cell in cells):
                continue
            values = [cell.value.strip() if isinstance(cell.value, str) else cell.value for cell in cells]
            invoice, period, purpose, note, extra = values
            message = None
            if extra is not None or any(cell.data_type in {"f", "e"} for cell in cells):
                message = "包含额外字段、公式或 Excel 错误值"
            elif not isinstance(invoice, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", invoice):
                message = "发票编号须为 1 至 64 位字母、数字、下划线或短横线"
            elif not isinstance(period, str) or not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", period) or period.startswith("0000"):
                message = "会计期间须为 YYYY-MM 文本"
            elif any(value is not None and (not isinstance(value, str) or len(value) > 500 or "\x00" in value)
                     for value in (purpose, note)):
                message = "用途和备注须为不超过 500 字的文本"
            elif invoice in seen:
                message = "文件中发票编号重复"
            if message:
                errors.append({"row": number, "message": message})
            else:
                rows.append({"row": number, "invoice_id": invoice, "accounting_period": period,
                             "purpose": purpose or None, "note": note or None})
            if isinstance(invoice, str):
                seen.add(invoice)
        if not rows and not errors:
            errors.append({"row": 1, "message": "没有可导入的发票"})
        return {"rows": rows, "errors": errors}
    finally:
        workbook.close()


@celery.task(bind=True, max_retries=3)
def parse_invoice(self, job_id, attempt, key):
    try:
        with s3().get_object(Bucket=BUCKET, Key=key)["Body"] as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES or hashlib.sha256(raw).hexdigest() != key.rsplit("/", 1)[-1][:-5]:
            result = {"rows": [], "errors": [{"row": 1, "message": "原件校验失败"}]}
        else:
            try:
                result = parse_xlsx(raw)
            except (ValueError, zipfile.BadZipFile, KeyError, OSError, SyntaxError):
                result = {"rows": [], "errors": [{"row": 1, "message": "无法解析工作簿，请检查格式、表头与行数"}]}
        request = urllib.request.Request(
            f"http://business:8080/internal/imports/{job_id}/{attempt}",
            data=json.dumps(result, ensure_ascii=False).encode(),
            headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=10) as response:
            response.read()
    except Exception as error:
        raise self.retry(exc=error, countdown=5)
