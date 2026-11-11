"""Back up the local product and verify restore in isolated containers."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ["docker", "compose", "--project-directory", str(ROOT / "product")]
DB = "tax_product"
TABLES = (
    "tenant", "enterprise", "product_invoice", "invoice_rule", "invoice_rule_review",
    "invoice_import", "invoice_proposal", "invoice_audit", "flyway_schema_history",
    "product_account", "tenant_membership", "enterprise_grant",
    "product_bank_transaction",
    "agent_chat_scope",
    "agent_task", "agent_message", "agent_trace", "agent_memory", "agent_memory_invalidation",
    "agent_memory_audit", "agent_operation",
)
S3 = """
import json, os, sys
import boto3
from botocore.config import Config
client = boto3.client("s3", endpoint_url="http://objects:8333",
    aws_access_key_id=os.environ["S3_USER"], aws_secret_access_key=os.environ["S3_PASSWORD"],
    config=Config(connect_timeout=3, read_timeout=10, s3={"addressing_style":"path"}))
bucket = "invoice-imports"
action, *args = sys.argv[1:]
if action == "list":
    keys = [item["Key"] for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket)
            for item in page.get("Contents", [])]
    print(json.dumps(sorted(keys)))
elif action == "get":
    with client.get_object(Bucket=bucket, Key=args[0])["Body"] as stream:
        sys.stdout.buffer.write(stream.read())
elif action == "put":
    client.put_object(Bucket=bucket, Key=args[0], Body=sys.stdin.buffer.read(),
        ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
"""


def run(args, data=None, env=None):
    result = subprocess.run(args, input=data, capture_output=True, env=env, timeout=120)
    if result.returncode:
        raise RuntimeError(f"{args[0]} {args[1]} failed: {result.stderr.decode('utf-8', errors='replace').strip()}")
    return result.stdout


def sha(data):
    return hashlib.sha256(data).hexdigest()


def inspect_image(container):
    return run(["docker", "inspect", "--format", "{{.Image}}", container]).decode().strip()


def table_snapshot(container):
    # Read all table rows in one SQL statement and use canonical JSONB ordering.
    fields = ", ".join(f"'{table}', (select coalesce(jsonb_agg(row_data order by row_data), '[]') "
                       f"from (select to_jsonb(t) row_data from public.{table} t) s)" for table in TABLES)
    return json.loads(run(["docker", "exec", container, "psql", "-X", "-v", "ON_ERROR_STOP=1",
                           "-U", DB, "-d", DB, "-At", "-c", f"select jsonb_build_object({fields})"]))


def summary(snapshot):
    return {name: {"rows": len(rows), "sha256": sha(json.dumps(rows, ensure_ascii=True, sort_keys=True,
                                                             separators=(",", ":")).encode())}
            for name, rows in snapshot.items()}


def wait_for(check):
    until = time.monotonic() + 75
    while time.monotonic() < until:
        try:
            return check()
        except RuntimeError:
            time.sleep(2)
    raise RuntimeError("Isolated restore services did not become ready")


def main():
    stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d-%H%M%S")
    tag = "p4-restore-" + uuid4().hex[:10]
    output = ROOT / "data" / "product-backups" / f"{stamp}-{tag}"
    output.mkdir(parents=True)
    source_db, source_objects, source_importer = [
        run(COMPOSE + ["ps", "-q", service]).decode().strip() for service in ("db", "objects", "importer")
    ]
    if not all((source_db, source_objects, source_importer)):
        raise RuntimeError("Start the source product before backing it up")
    images = {name: inspect_image(container) for name, container in
              zip(("db", "objects", "importer"), (source_db, source_objects, source_importer))}
    source_s3 = ["docker", "exec", "-i", source_importer, "python", "-c", S3]

    before = table_snapshot(source_db)
    dump = run(["docker", "exec", source_db, "pg_dump", "-U", DB, "-d", DB, "-Fc", "--no-owner", "--no-acl"])
    (output / "postgres.dump").write_bytes(dump)
    objects = []
    for index, key in enumerate(json.loads(run(source_s3 + ["list"]))):
        content = run(source_s3 + ["get", key])
        filename = f"object-{index:04d}.xlsx"
        (output / filename).write_bytes(content)
        objects.append({"key": key, "file": filename, "bytes": len(content), "sha256": sha(content)})
    object_hashes = {item["key"]: item["sha256"] for item in objects}
    for job in before["invoice_import"]:
        expected_key = f"{job['tenant_id']}/{job['enterprise_id']}/{job['file_sha256']}.xlsx"
        if job["object_key"] != expected_key or object_hashes.get(expected_key) != job["file_sha256"]:
            raise RuntimeError(f"Source object mismatch for import {job['job_id']}")
    if before != table_snapshot(source_db) or sorted(object_hashes) != json.loads(run(source_s3 + ["list"])):
        raise RuntimeError("Source changed during capture; repeat during a quiet window")
    for key, digest in object_hashes.items():
        if sha(run(source_s3 + ["get", key])) != digest:
            raise RuntimeError("Source object changed during capture")
    manifest = {"database_sha256": sha(dump), "tables": summary(before), "objects": objects, "images": images}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Backup captured at {output}", flush=True)

    started = time.monotonic()
    db_name, object_name = tag + "-db", tag + "-objects"
    env = {**os.environ, "POSTGRES_PASSWORD": secrets.token_hex(24), "S3_USER": "restore",
           "S3_PASSWORD": secrets.token_hex(24), "AWS_ACCESS_KEY_ID": "restore"}
    env["AWS_SECRET_ACCESS_KEY"] = env["S3_PASSWORD"]
    containers = []
    network = False
    try:
        run(["docker", "network", "create", "--internal", tag])
        network = True
        run(["docker", "run", "-d", "--name", db_name, "--network", tag,
             "-v", "/var/lib/postgresql/data", "-e", "POSTGRES_PASSWORD", "-e", f"POSTGRES_USER={DB}",
             "-e", f"POSTGRES_DB={DB}", images["db"]], env=env)
        containers.append(db_name)
        run(["docker", "run", "-d", "--name", object_name, "--network", tag, "--network-alias", "objects",
             "-v", "/data", "-e", "AWS_ACCESS_KEY_ID", "-e", "AWS_SECRET_ACCESS_KEY", "-e", "S3_BUCKET=invoice-imports",
             images["objects"], "mini", "-dir=/data"], env=env)
        containers.append(object_name)
        wait_for(lambda: run(["docker", "exec", db_name, "pg_isready", "-h", "127.0.0.1", "-U", DB, "-d", DB]))
        restore_s3 = ["docker", "run", "--rm", "-i", "--network", tag, "-e", "S3_USER", "-e", "S3_PASSWORD",
                      "--entrypoint", "python", images["importer"], "-c", S3]
        if json.loads(wait_for(lambda: run(restore_s3 + ["list"], env=env))) != []:
            raise RuntimeError("Restore bucket is not empty")
        restored_dump = (output / "postgres.dump").read_bytes()
        if sha(restored_dump) != manifest["database_sha256"]:
            raise RuntimeError("Backup dump checksum changed")
        run(["docker", "exec", "-i", db_name, "pg_restore", "--exit-on-error", "--single-transaction",
             "--no-owner", "--no-acl", "-U", DB, "-d", DB], restored_dump)
        for item in objects:
            content = (output / item["file"]).read_bytes()
            if sha(content) != item["sha256"]:
                raise RuntimeError("Backup object checksum changed")
            run(restore_s3 + ["put", item["key"]], content, env)
        after = table_snapshot(db_name)
        if before != after:
            raise RuntimeError(f"Restored tables differ: {[table for table in TABLES if before[table] != after[table]]}")
        if json.loads(run(restore_s3 + ["list"], env=env)) != sorted(object_hashes):
            raise RuntimeError("Restored bucket keys differ")
        for item in objects:
            content = run(restore_s3 + ["get", item["key"]], env=env)
            if len(content) != item["bytes"] or sha(content) != item["sha256"]:
                raise RuntimeError(f"Restored object mismatch: {item['key']}")
        report = {"status": "passed", "isolation_network": tag, "restore_seconds": round(time.monotonic() - started, 2),
                  "tables": summary(after), "objects_verified": len(objects), "job_refs_verified": len(after["invoice_import"]),
                  "backup_bytes": len(dump) + sum(item["bytes"] for item in objects)}
        (output / "restore-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))
    finally:
        # Remove only containers created by this invocation and their anonymous volumes.
        for name in reversed(containers):
            run(["docker", "rm", "-fv", name])
        if network:
            run(["docker", "network", "rm", tag])


if __name__ == "__main__":
    main()
