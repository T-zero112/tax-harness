"""Bounded LangGraph decision loop with a Java-only business gateway."""
import json
import os
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
            state["memory"], state["evidence"], state["feedback"], state["requirements"], state["memory"]["trace"]))
        state["memory"]["trace"]["decisions"].append(decision.model_dump())
        return {"decision": decision, "steps": state["steps"] + 1, "route": "validate", "events": []}
    except (ValueError, KeyError, TypeError) as error:
        if str(error) == "context_budget_exceeded":
            return {"route": "finish", "events": [event("error", {"code": "context_budget_exceeded",
                    "message": "当前任务与证据超出上下文预算，请缩小问题。待办已保留。"}),
                    event("done", {"status": "incomplete"})]}
        state["memory"]["trace"]["decisions"].append({"kind": "invalid", "code": "invalid_model_response"})
        return {"steps": state["steps"] + 1, "route": "decide", "feedback": "invalid_model_response",
                "events": [event("status", {"code": "invalid_model_response", "message": "模型决策无效，正在重试。"})]}
    except httpx.HTTPError:
        state["memory"]["trace"]["decisions"].append({"kind": "invalid", "code": "model_unavailable"})
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
        claim = guards.proposal_claim(decision, state["memory"]["trace"].get("events", []), state["evidence"])
        if claim:
            raise ValueError(claim)
        reason = guards.safe_reply(decision.message, state["evidence"], state["memory"]["trace"].get("events", []))
        if reason:
            raise ValueError(reason)
        if decision.kind == "ask":
            if re.search(r"已(?:经)?(?:保存|写入|修改|补充|提出|生成)|建议已", decision.message) and not any(e.get("event")=="proposal" and e.get("data",{}).get("proposal",{}).get("proposal_id") and e.get("data",{}).get("operation_id") for e in state["memory"]["trace"].get("events",[])):
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
            "proposal_claim_without_event", "proposal_evidence_missing", "empty_reply", "answer_items_incomplete", "evidence_mismatch",
            "direction_mismatch", "filter_mismatch", "invoice_amount_unknown", "list_incomplete",
            "evidence_conflict", "requirements_missing", "requirements_fixed", "duplicate_requirements"
        } else "invalid_arguments"
        message = "本轮没有生成提案。要修改就调用 propose 工具，否则如实说明尚未生成。" if code == "proposal_claim_without_event" else "当前答案或操作缺少匹配证据，正在重新核对。"
        events = [event("status", {"code":code,"message":message})]
        if code == "proposal_claim_without_event":
            events.append(event("status", {"code":"proposal_evidence_missing","message":message,"alias_of":code}))
        return {"route":"decide","feedback":message if code=="proposal_claim_without_event" else code,
                "requirements":requirements,"registered":registered,"events":events}


def execute(state):
    name, arguments = state["tool"], state["arguments"]
    tool_started = time.monotonic()
    events = [event("tool_call", {"tool": name, "arguments": arguments})]
    try:
        result = tools.invoke(name, arguments, state["body"]["call_token"])
    except (httpx.HTTPError, ValueError):
        result = {"status": "unknown" if name == "create_proposal" else "failed",
                  "error": {"code": "tool_unavailable", "message": "业务调用结果待核实，请查询后重试。"}, "data": {}}
    events.append(event("tool_result", {"tool": name, "result": result,
                                         "duration_ms": int((time.monotonic()-tool_started)*1000)}))
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
    started = time.monotonic()
    trace = {"decisions": [], "events": [], "context_snapshots": [], "trimming": [], "context_bytes": 0,
             "context_budget": max(4096,min(48000,int(os.getenv("PRODUCT_CONTEXT_BUDGET","12000"))))}
    final = {"status": "incomplete"}
    reply = ""
    try:
        memory = context.load(body["call_token"])
        memory["trace"] = trace
        yield event("status", {"message": "已读取持久化任务与有效偏好，正在核对资料。",
                              "task_id": body["task_id"], "turn_id": body["turn_id"],
                              "message_id": memory.get("message_id")})
        if guards.scope_conflict(body["message"], scope, body["enterprises"]):
            tools.invoke("invalidate_scope", {}, body["call_token"])
            entries = [event("error", {"code": "scope_conflict",
                "message": "消息中的企业与当前范围不同，请先切换企业。原未确认提案已失效。"}),
                       event("done", {"status": "needs_input"})]
            updates = [{"scope_guard": {"events": entries}}]
        else:
            initial = {"body": body, "scope": scope, "memory": memory, "steps": 0, "evidence": [],
                       "requirements": guards.required_by_text(body["message"]), "registered": False,
                       "feedback": "", "events": [], "started": started}
            updates = graph.stream(initial, {"recursion_limit": 30}, stream_mode="updates")
        for update in updates:
            for values in update.values():
                additions = []
                if values.get("decision") is not None:
                    additions.append(event("decision", {"step":len(trace["decisions"]),
                        "kind":values["decision"].kind}))
                if "requirements" in values:
                    additions.append(event("requirements", {"items":[r.model_dump() for r in values["requirements"]]}))
                for entry in additions:
                    trace["events"].append(entry)
                    yield entry
                reply = values.get("reply", reply)
                for entry in values.get("events", []):
                    trace["events"].append(entry)
                    if entry["event"] == "proposal":
                        reply = "发票 " + entry["data"]["invoice_id"] + " 的提案待确认，请核对后点击确认保存。"
                    if entry["event"] == "error" and not reply:
                        reply = entry["data"].get("message", "本轮未完成，待办已保留。")
                    if entry["event"] == "done":
                        final = entry["data"]
                    else:
                        yield entry
    except Exception:
        entry = event("error", {"code": "turn_failed", "message": "本轮未完成，待办与业务结果请重新核实。"})
        trace["events"].append(entry)
        yield entry
    trace.update(status=final["status"], reply=reply, steps=len(trace["decisions"]),
                 model_name=os.getenv("PRODUCT_MODEL_NAME", ""),
                 duration_ms=int((time.monotonic() - started) * 1000),
                 guard_codes=[e["data"]["code"] for e in trace["events"]
                              if e["event"] in ("status", "error") and "code" in e["data"]])
    try:
        result = tools.invoke("trace", trace, body["call_token"])
        if result["status"] != "succeeded":
            final = {"status": "incomplete"}
            yield event("error", {"code": "trace_unavailable", "message": "本轮轨迹未确认，请恢复任务核实。"})
    except Exception:
        final = {"status": "incomplete"}
        yield event("error", {"code": "trace_unavailable", "message": "本轮连接中断，任务可恢复后核实。"})
    yield event("done", {**final, "task_id": body["task_id"], "turn_id": body["turn_id"]})
