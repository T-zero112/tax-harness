"use client";
import { FormEvent, useState } from "react";

export type Enterprise = {enterprise_id: string; name: string; role: "admin" | "editor" | "viewer"};
type Invoice = {enterprise_id: string; invoice_id: string; purpose: string | null; note: string | null; revision: number};
export type RuleRef = {rule_id: string; code: string; source: string; version: string; clause: string};
type Proposal = {proposal_id: string; expected_revision: number; changes: Record<string, string>; rule_ref: RuleRef};
type Receipt = {proposal_id: string; actor: string; rule_version: string; rule_ref: RuleRef | null; before: Invoice; after: Invoice; saved_at: string};

export async function api(path: string, options?: RequestInit) {
  const response = await fetch(path, options);
  if (!response.ok) {
    const messages: Record<number, string> = {
      400: "输入或确认无效。", 401: "请重新登录。", 403: "当前账户没有修改权限。",
      404: "当前范围内没有找到该记录。", 409: "版本或确认记录冲突，请重新核验。",
    };
    throw new Error(messages[response.status] ?? "请求失败，请稍后重试。");
  }
  return response.status === 204 ? undefined : response.json();
}

export function post(body: unknown, headers: Record<string, string> = {}): RequestInit {
  return {method: "POST", headers: {"Content-Type": "application/json", ...headers}, body: JSON.stringify(body)};
}

function reference(rule: RuleRef) {
  return `${rule.source} · 版本 ${rule.version} · 条款 ${rule.clause}`;
}

function manualMessage(result: {reason: string; review_id: string}) {
  return `${result.reason === "rule_missing" ? "缺少适用规则" : "适用规则冲突"}，已转人工复核。复核编号 ${result.review_id}`;
}

export default function InvoiceWorkflow({enterprises, target}: {
  enterprises: Enterprise[]; target?: {enterprise_id: string; invoice_id: string};
}) {
  const [record, setRecord] = useState<Invoice | null>(null);
  const [path, setPath] = useState("");
  const [missing, setMissing] = useState<string[]>([]);
  const [rule, setRule] = useState<RuleRef | null>(null);
  const [audit, setAudit] = useState<Receipt[]>([]);
  const [proposal, setProposal] = useState<{value: Proposal; key: string} | null>(null);
  const [status, setStatus] = useState("");
  const [busy, setBusy] = useState(false);
  const role = enterprises.find(item => item.enterprise_id === record?.enterprise_id)?.role;
  const writable = role === "admin" || role === "editor";

  async function run(action: () => Promise<void>) {
    setBusy(true);
    setStatus("");
    try { await action(); }
    catch (error) { setStatus(error instanceof Error ? error.message : "请求失败。"); }
    finally { setBusy(false); }
  }

  function lookup(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const target = "/api/enterprises/" + encodeURIComponent(String(form.get("enterprise"))) +
      "/invoices/" + encodeURIComponent(String(form.get("invoice")).trim());
    setRecord(null);
    setProposal(null);
    void run(async () => {
      const invoice: Invoice = await api(target);
      const review = await api(target + "/review", {method: "POST"});
      const history = await api(target + "/audit");
      setPath(target);
      setRecord(invoice);
      setMissing(review.missing ?? []);
      setAudit(history);
      setRule(review.rule_ref ?? null);
      setStatus(review.status === "manual_review" ? manualMessage(review) : review.rule_description);
    });
  }

  function suggest(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const changes = Object.fromEntries(["purpose", "note"].flatMap(field => {
      const value = String(form.get(field) ?? "").trim();
      return value ? [[field, value]] : [];
    }));
    if (!Object.keys(changes).length) { setStatus("请填写至少一个补全值。"); return; }
    void run(async () => {
      const result = await api(path + "/proposals", post(changes));
      if (result.status === "manual_review") {
        setRule(null);
        setStatus(manualMessage(result));
        return;
      }
      const value: Proposal = result;
      setProposal({value, key: crypto.randomUUID()});
      setStatus("建议已生成，请核对后确认。");
    });
  }

  function confirm() {
    if (!proposal) return;
    void run(async () => {
      const receipt: Receipt = await api(path + "/proposals/" + proposal.value.proposal_id + "/confirm",
        post({confirmed: true}, {"Idempotency-Key": proposal.key}));
      let current: Invoice;
      try {
        current = await api(path);
        const review = await api(path + "/review", {method: "POST"});
        setMissing(review.missing ?? []);
        setRule(review.rule_ref ?? null);
        setAudit(await api(path + "/audit"));
        if (review.status === "manual_review") {
          setRecord(current);
          setProposal(null);
          setStatus("保存已确认。回查时" + manualMessage(review));
          return;
        }
      } catch {
        setStatus("保存已确认，回查未完成。可再次确认以核对原回执。");
        return;
      }
      setRecord(current);
      setProposal(null);
      const matches = current.revision === receipt.after.revision &&
        current.purpose === receipt.after.purpose && current.note === receipt.after.note;
      setStatus(matches ? "保存成功，回查与确认结果一致。" : "本次保存已入账，回查发现记录随后发生变化。");
    });
  }

  return <section id="invoice-workflow">
    <h2>发票核验与补全</h2>
    <form onSubmit={lookup}>
      <label>企业 <select name="enterprise" defaultValue={target?.enterprise_id}>{enterprises.map(e =>
        <option value={e.enterprise_id} key={e.enterprise_id}>{e.name}</option>)}</select></label>{" "}
      <label>发票编号 <input name="invoice" defaultValue={target?.invoice_id ?? "F001"} required /></label>{" "}
      <button disabled={busy}>查询并核验</button>
    </form>
    {record && <>
      <p>{record.enterprise_id} / {record.invoice_id} · 版本 {record.revision}</p>
      <p>用途 {record.purpose ?? "未填写"} · 备注 {record.note ?? "未填写"}</p>
      {rule && <p>依据 {reference(rule)} · 缺项 {missing.map(field => field === "purpose" ? "用途" : "备注").join("、") || "无"}</p>}
      {writable && rule && !proposal && <form onSubmit={suggest}>
        <label>补全用途 <input name="purpose" maxLength={500} /></label>{" "}
        <label>补全备注 <input name="note" maxLength={500} /></label>{" "}
        <button disabled={busy}>生成待确认建议</button>
      </form>}
      {!writable && <p>当前账户可查看与核验发票。</p>}
      {proposal && <div>
        <p>建议基于发票版本 {proposal.value.expected_revision}，依据 {reference(proposal.value.rule_ref)}</p>
        <ul>{Object.entries(proposal.value.changes).map(([field, value]) =>
          <li key={field}>{field === "purpose" ? "用途" : "备注"} → {value}</li>)}</ul>
        <button disabled={busy} onClick={confirm}>确认保存</button>{" "}
        <button disabled={busy} onClick={() => setProposal(null)}>返回编辑</button>
      </div>}
      <h3>保存审计</h3>
      <ul>{audit.map(entry => <li key={entry.proposal_id}><details>
        <summary>{entry.actor} · {entry.saved_at} · 版本 {entry.before.revision} → {entry.after.revision}</summary>
        <p>依据 {entry.rule_ref ? reference(entry.rule_ref) : entry.rule_version}</p>
        <p>用途 {entry.before.purpose ?? "空"} → {entry.after.purpose ?? "空"}</p>
        <p>备注 {entry.before.note ?? "空"} → {entry.after.note ?? "空"}</p>
      </details></li>)}</ul>
    </>}
    <p role="status">{status}</p>
  </section>;
}
