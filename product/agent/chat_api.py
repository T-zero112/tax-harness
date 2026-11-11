import json
import os
import queue
import secrets
import threading
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import Field

from executor import stream
from model_adapter import StrictModel, configured, redact

router = APIRouter()


class Scope(StrictModel):
    tenant_id: str
    username: str
    enterprise_id: str
    period: str = Field(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")
    generation: int


class Turn(StrictModel):
    session_id: UUID
    task_id: UUID
    turn_id: UUID
    scope: Scope
    message: str = Field(min_length=1, max_length=4000)
    call_token: str
    enterprises: list[dict]


@router.post("/internal/chat/turn")
def chat(body: Turn, authorization: str = Header(default="")):
    if not secrets.compare_digest(authorization.encode(), ("Bearer " + os.environ["AGENT_TOKEN"]).encode()):
        raise HTTPException(401)
    if not configured():
        raise HTTPException(503, "请配置 PRODUCT_MODEL_BASE_URL 和 PRODUCT_MODEL_NAME 后启动对话。")

    def events():
        output = queue.Queue()
        stopped = threading.Event()

        def run():
            try:
                for entry in stream(body.model_dump(mode="json")):
                    if stopped.is_set():
                        break
                    output.put(entry)
            finally:
                output.put(None)

        threading.Thread(target=run, daemon=True).start()
        try:
            while True:
                try:
                    entry = output.get(timeout=10)
                except queue.Empty:
                    yield ": heartbeat\n\n"
                    continue
                if entry is None:
                    break
                yield "event: " + entry["event"] + "\ndata: " + redact(json.dumps(entry["data"], ensure_ascii=False)) + "\n\n"
        finally:
            stopped.set()

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
