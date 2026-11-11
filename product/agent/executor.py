"""Bounded LangGraph decision loop with a Java-only business gateway."""
import json
import re
import time
from typing import TypedDict
from uuid import uuid4

import httpx
from langgraph.graph import StateGraph, START, END

import context
import guards
import model_adapter
import tools


class State(TypedDict, total=False):
    body: dict
    memory: dict
    scope: dict
    steps: int
    evidence: list
    requirements: list
    registered: bool
    feedback: str
    decision: object
    arguments: dict
    tool: str
    route: str
    events: list
    started: float
    reply: str


def event(name, data):
    return {"event": name, "data": data}


def decide(state):
    if state["steps"] >= 6 or time.monotonic() - state["started"] > 720:
        return {"route": "finish", "events": [event("error", {"code": "step_limit", "message": "本轮未完成，请缩小问题后重试。"}),
                                              event("done", {"status": "incomplete"})]}
    try:
        decision = model_adapter.decide(context.messages(state["scope"], state["body"]["message"],
            state["memory"], state["evidence"], state["feedback"], state["requirements"]))
        return {"decision": decision, "steps": state["steps"] + 1, "route": "validate", "events": []}
    except (ValueError, KeyError, TypeError):
        return {"steps": state["steps"] + 1, "route": "decide", "feedback": "invalid_model_response",
                "events": [event("status", {"code": "invalid_model_response", "message": "模型决策无效，正在重试。"})]}
    except httpx.HTTPError:
        return {"route": "finish", "events": [event("error", {"code": "model_unavailable", "message": "模型请求失败或超时，待办已保留。"}),
                                              event("done", {"status": "incomplete"})]}


def validate(state):
    decision = state["decision"]
    requirements = state["requirements"]
    registered = state["registered"]
    try:
        if not registered:
            requirements = list(requirements)
            proposed = decision.requirements or []
            for req in proposed:
                if not any(existing.kind == req.kind and existing.filters == req.filters for existing in requirements):
                    requirements.append(req)
            if len({r.requirement_id for r in requirements}) != len(requirements):
                raise ValueError("duplicate_requirements")
            registered = True
        elif decision.requirements is not None and decision.requirements != requirements:
            raise ValueError("requirements_fixed")
        update = {"requirements": requirements, "registered": registered}
        if decision.kind in ("tool", "propose"):
            name = "create_proposal" if decision.kind == "propose" else decision.tool
            if name not in tools.TOOLS:
                raise ValueError("unknown_tool")
            arguments = tools.TOOLS[name].model_validate(decision.arguments).model_dump(exclude_none=True)
            if name == "create_proposal":
                expected = guards.target(state["body"]["message"], state["memory"]["pending"])
                if expected != arguments["invoice_id"]:
                    raise ValueError("proposal_target_unverified")
                changes = arguments["changes"]
                user_text = "\n".join(state["memory"].get("pending_messages", [state["body"]["message"]]))
                if not changes or any(not v.strip() or len(v) > 500 or v not in user_text for v in changes.values()):
                    raise ValueError("proposal_value_unverified")
            return {**update, "tool": name, "arguments": arguments, "route": "execute", "events": []}
        reason = guards.safe_reply(decision.message, state["evidence"])
        if reason:
            raise ValueError(reason)
        if decision.kind == "ask":
            if re.search(r"已(?:经)?(?:保存|写入|修改|补充|提出|生成)|建议已", decision.message):
                raise ValueError("proposal_evidence_missing")
            if re.search(r"[¥￥]\s*\d|\d[\d,.]*\s*(?:元|万元|分)(?!钟)", decision.message):
                raise ValueError("amount_unverified")
            return {**update, "reply": decision.message, "route": "finish",
                    "events": [event("answer", {"message": decision.message, "status": "needs_input"}),
                               event("done", {"status": "needs_input"})]}
        rendered = guards.render(requirements, decision.answer_items, state["evidence"])
        if not requirements:
            # General conversation can ask for clarification; business success requires declared evidence.
            raise ValueError("requirements_missing")
        return {**update, "reply": rendered, "route": "finish",
                "events": [event("answer", {"message": rendered, "status": "succeeded"}),
                           event("done", {"status": "succeeded"})]}
    except (ValueError, TypeError, KeyError) as error:
        code = str(error) if str(error) in {
            "unknown_tool", "proposal_target_unverified", "proposal_value_unverified", "amount_unverified",
            "proposal_evidence_missing", "empty_reply", "answer_items_incomplete", "evidence_mismatch",
            "direction_mismatch", "filter_mismatch", "invoice_amount_unknown", "list_incomplete",
            "evidence_conflict", "requirements_missing", "requirements_fixed", "duplicate_requirements"
        } else "invalid_arguments"
        return {"route": "decide", "feedback": code, "requirements": requirements, "registered": registered,
                "events": [event("status", {"code": code, "message": "当前答案或操作缺少匹配证据，正在重新核对。"})]}


def execute(state):
    name, arguments = state["tool"], state["arguments"]
    events = [event("tool_call", {"tool": name, "arguments": arguments})]
    try:
        result = tools.invoke(name, arguments, state["body"]["call_token"])
    except (httpx.HTTPError, ValueError):
        result = {"status": "unknown" if name == "create_proposal" else "failed",
                  "error": {"code": "tool_unavailable", "message": "业务调用结果待核实，请查询后重试。"}, "data": {}}
    events.append(event("tool_result", {"tool": name, "result": result}))
    if result["status"] != "succeeded":
        # Never retry a potentially persisted proposal after a transport failure.
        if name == "create_proposal":
            return {"route": "finish", "events": events + [event("error", result.get("error", {})),
                    event("done", {"status": result["status"]})]}
        return {"feedback": result.get("error", {}).get("code", "tool_failed"), "route": "decide", "events": events}
    if name == "create_proposal":
        if result["data"]["proposal"].get("status") == "manual_review":
            return {"route": "finish", "events": events + [event("answer", {"message": "规则缺失或冲突，已登记人工复核。"}),
                    event("done", {"status": "needs_input"})]}
        state["memory"]["proposal"] = {"invoice_id": arguments["invoice_id"],
                                       "proposal_id": result["data"]["proposal"]["proposal_id"]}
        return {"route": "finish", "events": events + [event("proposal", result["data"]),
                event("done", {"status": "pending_confirmation"})]}
    proof = {"evidence_id": str(uuid4()), "tool": name, "arguments": arguments, "result": result}
    # Bound displayed detail, preserve whole-query aggregates separately.
    data = proof["result"]["data"]
    if "records" in data and len(data["records"]) > 12:
        data["records"] = data["records"][:12]
        data["has_more"] = True
    events[-1]["data"]["evidence_id"] = proof["evidence_id"]
    return {"evidence": state["evidence"] + [proof], "feedback": "", "route": "decide", "events": events}


builder = StateGraph(State)
builder.add_node("decide", decide)
builder.add_node("validate", validate)
builder.add_node("execute", execute)
builder.add_edge(START, "decide")
for node in ("decide", "validate", "execute"):
    builder.add_conditional_edges(node, lambda state: state["route"],
        {"decide": "decide", "validate": "validate", "execute": "execute", "finish": END})
graph = builder.compile()


def stream(body):
    scope = body["scope"]
    key = (scope["tenant_id"], scope["username"], body["session_id"])
    try:
        memory = context.acquire(key)
    except ValueError:
        yield event("error", {"code": "conversation_busy", "message": "会话繁忙，请稍后重试。"})
        yield event("done", {"status": "incomplete"})
        return
    try:
        signature = (scope["enterprise_id"], scope["period"], scope["generation"])
        if memory.get("scope") != signature:
            memory["history"], memory["pending"] = [], None
            memory["scope"] = signature
            memory.pop("proposal", None)
            memory["pending_messages"] = []
        if guards.scope_conflict(body["message"], scope, body["enterprises"]):
            tools.invoke("invalidate_scope", {}, body["call_token"])
            memory["pending"] = None
            yield event("error", {"code": "scope_conflict", "message": "消息中的企业与当前范围不同，请先切换企业。原未确认提案已失效。"})
            yield event("done", {"status": "needs_input"})
            return
        pending = guards.target(body["message"], memory["pending"])
        if pending != memory["pending"]:
            memory["pending_messages"] = []
            memory.pop("proposal", None)
        memory["pending"] = pending
        memory["pending_messages"] = (memory.get("pending_messages", []) + [body["message"]])[-8:]
        initial = {"body": body, "scope": scope, "memory": memory, "steps": 0, "evidence": [],
                   "requirements": guards.required_by_text(body["message"]), "registered": False,
                   "feedback": "", "events": [], "started": time.monotonic()}
        yield event("status", {"message": "正在核对当前企业和期间的资料。"})
        reply = ""
        for update in graph.stream(initial, {"recursion_limit": 30}, stream_mode="updates"):
            for values in update.values():
                for entry in values.get("events", []):
                    yield entry
                reply = values.get("reply", reply)
        memory["history"] = (memory["history"] + [{"user": body["message"], "assistant": reply}])[-8:]
    except Exception:
        yield event("error", {"code": "turn_failed", "message": "本轮未完成，业务结果需重新查询核实。"})
        yield event("done", {"status": "incomplete"})
    finally:
        memory["lock"].release()
