"""Current-turn evidence and proposal validation, adapted from the competition guards."""
import re
from copy import deepcopy
import unicodedata
from types import SimpleNamespace
from decimal import Decimal

from amount_guards import amounts_supported, proposal_targets
from model_adapter import Requirement

KINDS = {
    "invoice_total": ("list_invoices", "invoice_cents", "发票合计"),
    "income_total": ("list_bank_transactions", "income_cents", "银行收入"),
    "expense_total": ("list_bank_transactions", "expense_cents", "银行支出"),
    "rules": ("search_rules", "rules", "适用规则"),
    "invoices": ("list_invoices", "records", "发票明细"),
    "review": ("check_purpose", "review", "核验结果"),
    "operation": ("get_operation", "operation", "保存状态"),
}


def required_by_text(text, pending=None):
    specs=[("invoice_total",r"发票.{0,8}(合计|总额|汇总)"),
           ("income_total",r"(银行)?收入"),("expense_total",r"(银行)?支出"),("rules",r"规则|依据"),
           ("review",r"检查.{0,20}用途|用途.{0,12}(符合|核验)"),
           ("operation",r"是否.{0,4}保存|有没有保存|保存状态")]
    invoice=target(text,pending)
    return [Requirement(requirement_id="required_"+kind,kind=kind,
            filters={"invoice_id":invoice} if invoice and kind in ("review","operation") else {})
            for kind,pattern in specs if re.search(pattern,text)]


def next_tools(requirements,evidence,memory):
    recommendations=[]
    for req in requirements:
        tool,key,_=KINDS[req.kind]
        args=dict(req.filters)
        if req.kind=="rules":args.setdefault("query","发票用途")
        if req.kind=="operation":
            known=next((x for x in memory.get("recent_operations",[]) if not args.get("invoice_id") or x["invoice_id"]==args["invoice_id"]),None)
            if known:args.update(invoice_id=known["invoice_id"],proposal_id=str(known["proposal_id"]))
            else:tool="list_recent_operations";args={**args,"limit":5}
        valid=[]
        for proof in evidence:
            try:
                render([req],[__import__("model_adapter").AnswerItem(requirement_id=req.requirement_id,evidence_ids=[proof["evidence_id"]],value_key=key)],[proof]);valid.append(proof)
            except (ValueError,KeyError,TypeError):pass
        if not valid:recommendations.append({"requirement_id":req.requirement_id,"tool":tool,"arguments":args})
    return recommendations


def feedback(code,requirements=(),evidence=(),memory=None):
    messages={
      "proposal_claim_without_event":"本轮没有生成提案。要修改就调用 propose 工具，否则如实说明尚未生成。",
      "unknown_tool":"tool 字段只能取工具清单中的枚举值；ask 不是工具，请使用 kind=ask 和 message。",
      "requirements_fixed":"requirements 已由程序登记，后续不要再输出或改写；使用上下文中固定的 requirement_id。",
      "duplicate_requirements":"requirement_id 重复或与另一种需求冲突；每个编号必须唯一，不得覆盖已有需求。",
      "answer_items_incomplete":"answer_items 必须对固定需求逐项各提供一次，不能遗漏或重复；编号见 requirements。",
      "evidence_mismatch":"evidence_ids 或 value_key 与需求不匹配；只用本轮成功工具返回的证据，value_key 见需求类型映射。",
      "operation_target_unverified":"proposal_id 未在最近操作摘要或用户输入中确认。先 list_recent_operations，再用返回的真实编号 get_operation。",
      "proposal_target_unverified":"invoice_id 不是用户已唯一确认的目标；请用 kind=ask 追问票号，不得猜测。",
      "proposal_value_unverified":"changes 只能包含用户提供的 purpose/note 原文；缺内容应追问，不得自行补值。",
      "amount_unverified":"message 中金额没有本轮证据支持；查询对应工具，金额通过 answer_items 交给程序渲染。",
      "invalid_arguments":"arguments 不符合工具 Schema；检查字段名、必填项、枚举和类型，不能传企业/身份参数。",
      "direction_mismatch":"银行证据方向错误；收入使用 direction=income 或不设方向，支出使用 expense 或不设方向。",
      "filter_mismatch":"工具参数与固定 filters 不一致；按 requirements 中企业范围内的票号/销售方/项目条件重新查询。",
      "invoice_amount_unknown":"发票金额存在未知值；不能把缺失当零或宣称合计完整，请追问补齐原始资料。",
      "list_incomplete":"工具返回明细截断；不可宣称全部明细已完成，请缩小过滤范围。",
      "evidence_conflict":"同一需求的工具证据数值冲突；重新核实，不能择一拼接为成功答案。",
      "requirements_missing":"尚未登记业务需求；没有证据需求时请追问，不得直接宣布业务成功。",
      "proposal_evidence_missing":"未取得真实保存/提案证据；生成建议用 propose，保存状态用 get_operation。",
      "empty_reply":"message 缺少有意义内容；提供真实追问，或用本轮证据生成 answer_items。",
      "invalid_model_response":"输出一个符合动作 Schema 的 JSON；kind 只能为 tool/ask/answer/propose。",
    }
    base=messages.get(code,"校验失败（"+code+"）。保留已有需求与证据，不能绕过校验；缺信息使用 kind=ask。")
    hints=next_tools(requirements,evidence,memory or {})
    return {"code":code,"reason":base,"fixed_requirements":[r.model_dump() for r in requirements],"recommended_next_tools":hints}


def meaningful(text):
    return any(unicodedata.category(c)[0] in {"L", "N"} for c in text)


def target(text, pending):
    identifiers = re.findall(r"(?<![A-Za-z0-9_-])[A-Za-z]+[0-9]+(?:-[A-Za-z0-9_-]+)?(?![A-Za-z0-9_-])", text)
    identifiers = [v for v in identifiers if not re.fullmatch(r"C\d+", v, re.I)]
    mentioned, targets = proposal_targets(text, identifiers)
    if mentioned:
        return next(iter(targets)) if len(targets) == 1 else None
    return pending


def scope_conflict(text, scope, enterprises):
    if any(value != scope["enterprise_id"] for value in re.findall(r"\bC[0-9]{3}\b", text, re.I)):
        return True
    if any(row["enterprise_id"] != scope["enterprise_id"] and row["name"] in text for row in enterprises):
        return True
    # The demo uses Chinese ordering names. Ambiguous short names require a scope choice.
    for prefix in re.findall(r"([甲乙丙丁])(?:企业|公司|企)", text):
        matches = [row["enterprise_id"] for row in enterprises if row["name"].startswith(prefix)]
        if matches != [scope["enterprise_id"]]:
            return True
    return bool(re.search(r"(?:另一家|别的|其他)(?:企业|公司)", text))


def safe_reply(text, evidence, proposal_events=()):
    reads = []
    for item in evidence:
        data = deepcopy(item["result"].get("data") or {})
        data.update(data.get("summary", {}))
        for key in ("invoice_cents", "income_cents", "expense_cents"):
            if key in data:
                data[key] = int(data[key])
        for record in data.get("records", []):
            if record.get("amount_cents") is not None:
                record["amount_cents"] = int(record["amount_cents"])
        if isinstance(data.get("record"), dict) and data["record"].get("amount_cents") is not None:
            data["record"]["amount_cents"] = int(data["record"]["amount_cents"])
        args = SimpleNamespace(direction=item["arguments"].get("direction"))
        reads.append((item["tool"], args, SimpleNamespace(data=data)))
    if not meaningful(text):
        return "empty_reply"
    if re.search(r"已(?:经)?(?:保存|写入|修改|补充|提出|生成)|建议已", text):
        verified_proposal = any(e.get("event")=="proposal" and e.get("data",{}).get("proposal",{}).get("proposal_id") and e.get("data",{}).get("operation_id") for e in proposal_events)
        if not (verified_proposal and not re.search(r"保存|写入|更新|修改已完成", text)) and not any(item["tool"] == "get_operation" and item["result"]["data"]["operation"]["state"] == "confirmed"
                   for item in evidence):
            return "proposal_evidence_missing"
    if not amounts_supported(text, reads):
        return "amount_unverified"
    return None


def render(requirements, items, evidence):
    wanted = {r.requirement_id for r in requirements}
    ids = [item.requirement_id for item in items]
    if len(ids) != len(set(ids)) or set(ids) != wanted:
        raise ValueError("answer_items_incomplete")
    by_id = {r["evidence_id"]: r for r in evidence}
    lines = []
    for req in requirements:
        item = next(i for i in items if i.requirement_id == req.requirement_id)
        tool, key, label = KINDS[req.kind]
        if item.value_key != key or not item.evidence_ids:
            raise ValueError("evidence_mismatch")
        values = []
        for identifier in item.evidence_ids:
            proof = by_id.get(identifier)
            if proof is None or proof["tool"] != tool:
                raise ValueError("evidence_mismatch")
            expected = dict(req.filters)
            actual = dict(proof["arguments"])
            if req.kind == "operation" and "proposal_id" not in expected:
                if str(proof["result"].get("data",{}).get("operation",{}).get("proposal_id")) != str(actual.get("proposal_id")):
                    raise ValueError("evidence_mismatch")
                actual.pop("proposal_id",None)
            if req.kind == "rules" and not expected:
                actual.pop("query", None)
            if req.kind in ("income_total", "expense_total"):
                direction = "income" if req.kind == "income_total" else "expense"
                if actual.get("direction") not in (None, direction):
                    raise ValueError("direction_mismatch")
                actual.pop("direction", None)
                expected.pop("direction", None)
            if actual != expected:
                raise ValueError("filter_mismatch")
            data = proof["result"]["data"]
            if req.kind == "invoice_total" and int(data["summary"]["unknown_amounts"]) > 0:
                raise ValueError("invoice_amount_unknown")
            value = data.get("summary", {}).get(key, data.get(key))
            if key == "review":
                value = data
            if value is None:
                raise ValueError("evidence_mismatch")
            if key == "records" and data.get("has_more"):
                raise ValueError("list_incomplete")
            values.append(value)
        if any(value != values[0] for value in values):
            raise ValueError("evidence_conflict")
        value = values[0]
        if key.endswith("_cents"):
            lines.append(f"{label} {Decimal(value)/100:,.2f} 元")
        elif key == "rules":
            if not value:
                lines.append("当前范围没有适用规则，请提供材料供人工复核。")
            for hit in value:
                rule = hit["rule"]
                cite = rule["citation"]
                lines.append(f'{cite["source"]} · 版本 {cite["version"]} · 条款 {cite["clause"]}：{rule["content"]}')
        else:
            import json
            lines.append(label + " " + json.dumps(value, ensure_ascii=False))
    return "\n".join(lines)


# Structured claims are checked against gateway events, never against the model flag alone.
def proposal_claim(decision, events, evidence=()):
    real = any(e.get("event") == "proposal" and e.get("data", {}).get("proposal", {}).get("proposal_id")
               and e.get("data", {}).get("operation_id") for e in events)
    text = decision.message
    completion = re.search(r"(?:已(?:经)?[^。！？\n]{0,60}(?:生成|提交|创建|保存|更新|完成|准备好)|(?:建议|提案|修改)[^。！？\n]{0,15}已(?:提交|生成|完成|保存))", text)
    # A verified operation read can prove an existing save; it does not prove a new proposal this turn.
    saved = any(p.get("tool")=="get_operation" and p.get("result",{}).get("status")=="succeeded" and p.get("result",{}).get("data",{}).get("operation",{}).get("state")=="confirmed" for p in evidence)
    existing_save = saved and bool(re.search(r"保存|写入|更新",text)) and not re.search(r"生成|提交|创建|准备好",text)
    if getattr(decision,"claims_proposal",False) and not real:
        return "proposal_claim_without_event"
    if completion and not real and not existing_save:
        return "proposal_claim_without_event"
    return None
