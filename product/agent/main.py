import os
import secrets
from functools import lru_cache
from pathlib import Path
from typing import Literal, TypedDict

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field
from fastembed import TextEmbedding


class ProbeState(TypedDict):
    message: str


def respond(state: ProbeState) -> ProbeState:
    return {"message": "Agent 已收到 Java 转发的联通性检查。"}


builder = StateGraph(ProbeState)
builder.add_node("respond", respond)
builder.add_edge(START, "respond")
builder.add_edge("respond", END)
graph = builder.compile()
app = FastAPI()
token = os.environ["AGENT_TOKEN"]


class Probe(BaseModel):
    tenant_id: str
    enterprise_id: str


class InvoiceSnapshot(BaseModel):
    enterprise_id: str
    invoice_id: str
    purpose: str | None
    note: str | None
    revision: int


class ReviewRequest(BaseModel):
    invoice: InvoiceSnapshot
    changes: dict[Literal["purpose", "note"], str]
    required_fields: list[Literal["purpose", "note"]]


class ReviewState(TypedDict, total=False):
    request: ReviewRequest
    result: dict


def check_invoice(state: ReviewState) -> ReviewState:
    body = state["request"]
    return {"result": {
        "missing": [field for field in body.required_fields if not (getattr(body.invoice, field) or "").strip()],
        "changes": body.changes,
    }}


review_builder = StateGraph(ReviewState)
review_builder.add_node("check_invoice", check_invoice)
review_builder.add_edge(START, "check_invoice")
review_builder.add_edge("check_invoice", END)
review_graph = review_builder.compile()


def authorize(authorization: str):
    if not secrets.compare_digest(authorization.encode(), ("Bearer " + token).encode()):
        raise HTTPException(401)


class EmbedRequest(BaseModel):
    text: str = Field(min_length=1, max_length=600)


@lru_cache(maxsize=1)
def embedding_model():
    local = Path("/models")
    options = {"specific_model_path": str(local)} if (local / "model_optimized.onnx").is_file() else {}
    return TextEmbedding("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
                         cache_dir="/embedding-cache", threads=2, **options)


@app.post("/internal/embed")
def embed(body: EmbedRequest, authorization: str = Header(default="")):
    authorize(authorization)
    return {"embedding": next(embedding_model().embed([body.text])).tolist()}


@app.post("/internal/invoice-review")
def review(body: ReviewRequest, authorization: str = Header(default="")):
    authorize(authorization)
    return review_graph.invoke({"request": body})["result"]


@app.get("/health")
def health():
    return {"status": "UP"}


@app.post("/internal/probe")
def probe(body: Probe, authorization: str = Header(default="")):
    authorize(authorization)

    def events():
        yield "event: status\ndata: Agent 连接成功\n\n"
        result = graph.invoke({"message": ""})
        yield f"event: done\ndata: {result['message']}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")
