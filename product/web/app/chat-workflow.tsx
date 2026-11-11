"use client";
import {FormEvent, useEffect, useRef, useState} from "react";
import {api, post, type Enterprise, type RuleRef} from "./invoice-workflow";

type Entry = {event: string; data: Record<string, unknown>};
type Proposal = {
  invoice_id: string;
  before: {purpose: string | null; note: string | null; revision: number};
  proposal: {proposal_id: string; expected_revision: number; changes: Record<string, string>; rule_ref: RuleRef};
};

export default function ChatWorkflow({enterprises}: {enterprises: Enterprise[]}) {
  const [enterprise, setEnterprise] = useState(enterprises[0].enterprise_id);
  const [period, setPeriod] = useState("2026-09");
  const [session, setSession] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");
  const [lines, setLines] = useState<string[]>([]);
  const [trace, setTrace] = useState<Entry[]>([]);
  const [proposal, setProposal] = useState<Proposal | null>(null);
  const key = useRef("");
  const inFlight = useRef(false);

  useEffect(() => {setSession(crypto.randomUUID());}, []);
  const base = "/api/enterprises/" + encodeURIComponent(enterprise);

  async function switchScope(nextEnterprise: string, nextPeriod: string) {
    if (inFlight.current || !session) return;
    inFlight.current = true;
    setBusy(true);
    setProposal(null);
    try {
      await api("/api/enterprises/" + encodeURIComponent(nextEnterprise) + "/chat/scope",
        post({session_id: session, period: nextPeriod}));
      setEnterprise(nextEnterprise);
      setPeriod(nextPeriod);
      setLines([]);
      setTrace([]);
      setStatus("范围已切换，请在当前企业和月份发起查询。");
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "范围切换失败。");
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }

  async function send(event: FormEvent) {
    event.preventDefault();
    if (inFlight.current || !message.trim() || !session) return;
    inFlight.current = true;
    setBusy(true);
    setProposal(null);
    setTrace([]);
    setLines(old => [...old, "我：" + message]);
    try {
      const response = await fetch(base + "/chat", post({session_id: session, period, message}));
      if (response.status === 503) throw new Error("对话模型尚未配置，请配置模型后重试。");
      if (!response.ok || !response.body) throw new Error("对话请求失败，请检查登录、企业权限或输入。");
      setMessage("");
      const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
      let buffer = "", completed = false;
      while (true) {
        const chunk = await reader.read();
        if (chunk.done) break;
        buffer += chunk.value.replace(/\r\n/g, "\n");
        const frames = buffer.split("\n\n");
        buffer = frames.pop() ?? "";
        for (const frame of frames) {
          const name = frame.split("\n").find(line => line.startsWith("event: "))?.slice(7);
          const raw = frame.split("\n").filter(line => line.startsWith("data: ")).map(line => line.slice(6)).join("\n");
          if (!name || !raw) continue;
          const data = JSON.parse(raw);
          setTrace(old => [...old, {event: name, data}]);
          if (name === "answer") setLines(old => [...old, "助手：" + data.message]);
          if (name === "status" || name === "error") setStatus(data.message ?? data.code);
          if (name === "proposal") {
            key.current = crypto.randomUUID();
            setProposal(data);
            setStatus("请核对建议内容，确认后保存。");
          }
          if (name === "done") {
            completed = true;
            if (data.status === "incomplete") setStatus("本轮未完成，请查看过程或缩小查询范围。");
          }
        }
      }
      if (!completed) throw new Error("连接中断，请重新查询核实结果。");
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "对话失败。");
    } finally {
      setBusy(false);
      inFlight.current = false;
    }
  }

  async function confirm() {
    if (!proposal || inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    const target = base + "/invoices/" + encodeURIComponent(proposal.invoice_id);
    try {
      const receipt = await api(target + "/proposals/" + proposal.proposal.proposal_id + "/confirm",
        post({confirmed: true}, {"Idempotency-Key": key.current}));
      const current = await api(target);
      if (current.revision !== receipt.after.revision ||
          Object.keys(proposal.proposal.changes).some(field => current[field] !== receipt.after[field]))
        throw new Error("保存回查出现差异，请核对审计记录。");
      setLines(old => [...old, "保存已确认，回查版本 " + current.revision + "。"]);
      setProposal(null);
      setStatus("保存与回查通过。");
    } catch (error) {
      setStatus((error instanceof Error ? error.message : "保存结果待核实。") +
        " 再次确认将复用同一幂等键。");
    } finally {
      setBusy(false);
      inFlight.current = false;
    }
  }

  return <section aria-labelledby="chat-heading" style={{borderTop:"1px solid #ddd", paddingTop:20, marginTop:24}}>
    <h2 id="chat-heading">对话</h2>
    <div style={{display:"flex", gap:12, flexWrap:"wrap"}}>
      <label>对话企业 <select value={enterprise} disabled={busy || !session}
        onChange={e => switchScope(e.target.value, period)}>
        {enterprises.map(item => <option key={item.enterprise_id} value={item.enterprise_id}>{item.name}</option>)}
      </select></label>
      <label>对话月份 <input type="month" value={period} disabled={busy || !session}
        onChange={e => {if (e.target.value) switchScope(enterprise, e.target.value);}} /></label>
    </div>
    <div aria-live="polite" style={{whiteSpace:"pre-wrap", overflowWrap:"anywhere"}}>
      {lines.map((line, index) => <p key={index}>{line}</p>)}
    </div>
    <form onSubmit={send}>
      <label htmlFor="chat-message">查询或补全要求</label>
      <textarea id="chat-message" value={message} maxLength={4000} required disabled={busy}
        onChange={e => setMessage(e.target.value)} style={{display:"block", width:"100%", minHeight:80}} />
      <button disabled={busy || !session} style={{marginTop:8, color:"#002FA7"}}>发送</button>
    </form>
    <p role="status">{status}</p>
    {proposal && <article aria-label="待确认的发票建议" style={{border:"1px solid #002FA7", padding:16}}>
      <h3>发票 {proposal.invoice_id} · 待确认</h3>
      <p>读取版本 {proposal.proposal.expected_revision}</p>
      <table><thead><tr><th>字段</th><th>改前</th><th>改后</th></tr></thead>
        <tbody>{Object.entries(proposal.proposal.changes).map(([field, value]) =>
          <tr key={field}><td>{field === "purpose" ? "用途" : "备注"}</td>
            <td>{proposal.before[field as "purpose" | "note"] ?? "未填写"}</td><td>{value}</td></tr>)}</tbody></table>
      <p>{proposal.proposal.rule_ref.source} · 版本 {proposal.proposal.rule_ref.version} · 条款 {proposal.proposal.rule_ref.clause}</p>
      <button onClick={confirm} disabled={busy}>确认保存</button>
      <button onClick={() => setProposal(null)} disabled={busy}>收起建议</button>
    </article>}
    <details><summary>本轮工具与核对过程</summary>
      <pre style={{whiteSpace:"pre-wrap",overflowWrap:"anywhere"}}>{JSON.stringify(trace, null, 2)}</pre>
    </details>
  </section>;
}
