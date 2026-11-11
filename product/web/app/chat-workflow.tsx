"use client";
import {FormEvent, useEffect, useRef, useState} from "react";
import {api, post, type Enterprise, type RuleRef} from "./invoice-workflow";

type Entry = {event: string; data: Record<string, unknown>};
type Proposal = {
  invoice_id: string;
  before: {purpose: string | null; note: string | null; revision: number};
  proposal: {proposal_id: string; expected_revision: number; changes: Record<string, string>; rule_ref: RuleRef};
};
type Task = {task_id: string; revision: number; accounting_period: string; status: string; question: string | null; bound_scope_id: string};
type Message = {message_id: string; role: string; content: string};
type Memory = {memory_id: string; content: string; revision: number; source_message_id: string};
type Operation = {operation_id: string; kind: string; state: string; idempotency_key: string | null};

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
  const [task, setTask] = useState<Task | null>(null);
  const [matchesPeriod, setMatchesPeriod] = useState(true);
  const [resumed, setResumed] = useState(false);
  const [messages, setMessages] = useState<Message[]>([]);
  const [memories, setMemories] = useState<Memory[]>([]);
  const [operations, setOperations] = useState<Operation[]>([]);
  const [memoryContent, setMemoryContent] = useState("");
  const [sourceMessage, setSourceMessage] = useState("");
  const [userWide, setUserWide] = useState(false);
  const key = useRef("");
  const inFlight = useRef(false);

  useEffect(() => {setSession(crypto.randomUUID());}, []);
  useEffect(() => {
    if (session) refresh(enterprise, period).catch(error => setStatus(error.message));
  }, [session]);
  const base = "/api/enterprises/" + encodeURIComponent(enterprise);

  async function refresh(nextEnterprise: string, nextPeriod: string, currentScope?: string) {
    const target = "/api/enterprises/" + encodeURIComponent(nextEnterprise) + "/chat";
    const [active, preferences] = await Promise.all([
      api(target + "/tasks/active?period=" + encodeURIComponent(nextPeriod)), api(target + "/memories"),
    ]);
    setTask(active.task);
    setMemories(preferences);
    setMessages(active.messages ?? []);
    setOperations(active.unfinished_operations ?? []);
    setMatchesPeriod(active.matches_period ?? true);
    setResumed(!!active.task && !active.requires_resume && active.task.bound_scope_id === (currentScope ?? session));
    setProposal(active.matches_period ? active.proposal : null);
    const pending = (active.unfinished_operations ?? []).find((op: Operation) => op.kind === "confirmation");
    if (pending?.idempotency_key) key.current = pending.idempotency_key;
    else if (active.proposal && !key.current) key.current = crypto.randomUUID();
    if (active.task && !active.matches_period) setStatus("原任务属于 " + active.task.accounting_period + "，当前月份仅执行查询。");
    else if (active.task?.question) setStatus(active.task.question);
  }

  async function resume() {
    if (!task || inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    try {
      await api(base + "/chat/tasks/" + task.task_id + "/resume",
        post({session_id: session, period, expected_revision: task.revision}));
      await refresh(enterprise, period, session);
      key.current = key.current || crypto.randomUUID();
      setStatus("任务已恢复。请重新核对建议，点击确认后才会保存。");
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "恢复失败。");
    } finally { inFlight.current = false; setBusy(false); }
  }

  async function cancel() {
    if (!task || inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    try {
      await api(base + "/chat/tasks/" + task.task_id + "/cancel", post({expected_revision: task.revision}));
      await refresh(enterprise, period);
      setStatus("任务已取消。");
    } catch (error) { setStatus(error instanceof Error ? error.message : "取消失败。"); }
    finally { inFlight.current = false; setBusy(false); }
  }

  async function reconcile(operation: Operation) {
    if (!task || inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    try {
      const result = await api(base + "/chat/tasks/" + task.task_id + "/operations/" + operation.operation_id + "/reconcile", post({}));
      await refresh(enterprise, period);
      setStatus("操作核实结果：" + result.state);
    } catch (error) { setStatus(error instanceof Error ? error.message : "核实失败。"); }
    finally { inFlight.current = false; setBusy(false); }
  }

  async function saveMemory(existing?: Memory, deleting = false) {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    try {
      const path = base + "/chat/memories" + (existing ? "/" + existing.memory_id + (deleting ? "" : "/correct") : "");
      const body = existing ? {content: memoryContent, expected_revision: existing.revision}
        : {content: memoryContent, source_message_id: sourceMessage, user_wide: userWide};
      await api(path, {...post(body), method: deleting ? "DELETE" : "POST"});
      setMemoryContent("");
      setSourceMessage("");
      await refresh(enterprise, period);
      setStatus(deleting ? "偏好已删除。" : "偏好已保存。");
    } catch (error) { setStatus(error instanceof Error ? error.message : "偏好操作失败。"); }
    finally { inFlight.current = false; setBusy(false); }
  }

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
      key.current = "";
      await refresh(nextEnterprise, nextPeriod);
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
      const response = await fetch(base + "/chat", post({session_id: session, task_id: task?.task_id, period, message}));
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
      await refresh(enterprise, period, session);
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
        post({confirmed: true}, {"Idempotency-Key": key.current, "X-Chat-Scope": session}));
      const current = await api(target);
      if (current.revision !== receipt.after.revision ||
          Object.keys(proposal.proposal.changes).some(field => current[field] !== receipt.after[field]))
        throw new Error("保存回查出现差异，请核对审计记录。");
      setLines(old => [...old, "保存已确认，回查版本 " + current.revision + "。"]);
      setProposal(null);
      setStatus("保存与回查通过。");
      await refresh(enterprise, period);
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
    {task && <p>任务 {task.task_id} · {task.status} · {task.accounting_period}
      <button disabled={busy || !matchesPeriod} onClick={resume}>恢复当前任务</button>
      <button disabled={busy} onClick={cancel}>取消当前任务</button>
    </p>}
    {operations.map(operation => <p key={operation.operation_id}>待核实操作 {operation.operation_id} · {operation.state}
      <button disabled={busy} onClick={() => reconcile(operation)}>核实操作结果</button></p>)}
    <div aria-live="polite" style={{whiteSpace:"pre-wrap", overflowWrap:"anywhere"}}>
      {lines.map((line, index) => <p key={index}>{line}</p>)}
    </div>
    <form onSubmit={send}>
      <label htmlFor="chat-message">查询或补全要求</label>
      <textarea id="chat-message" value={message} maxLength={4000} required disabled={busy}
        onChange={e => setMessage(e.target.value)} style={{display:"block", width:"100%", minHeight:80}} />
      <button disabled={busy || !session || !!proposal && !resumed} style={{marginTop:8, color:"#002FA7"}}>发送</button>
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
      <button onClick={confirm} disabled={busy || !resumed || !matchesPeriod || operations.length > 0}>确认保存</button>
      <button onClick={() => setProposal(null)} disabled={busy}>收起建议</button>
    </article>}
    <details><summary>偏好</summary>
      <p>偏好用于表达方式和默认值，保存操作仍需逐次确认。</p>
      <label>来源消息 <select value={sourceMessage} onChange={event => setSourceMessage(event.target.value)} disabled={busy}>
        <option value="">选择本人发出的消息</option>
        {messages.filter(item => item.role === "user").map(item =>
          <option key={item.message_id} value={item.message_id}>{item.content.slice(0, 70)}</option>)}
      </select></label>
      <label>偏好内容 <input value={memoryContent} maxLength={1000} disabled={busy}
        onChange={event => setMemoryContent(event.target.value)} /></label>
      <label><input type="checkbox" checked={userWide} disabled={busy}
        onChange={event => setUserWide(event.target.checked)} />用于本人所有已授权企业</label>
      <button disabled={busy || !sourceMessage || !memoryContent.trim()} onClick={() => saveMemory()}>新增偏好</button>
      <ul>{memories.map(memory => <li key={memory.memory_id}>{memory.content} · 版本 {memory.revision}
        <button disabled={busy || !memoryContent.trim()} onClick={() => saveMemory(memory)}>用输入内容更正</button>
        <button disabled={busy} onClick={() => saveMemory(memory, true)}>删除</button>
      </li>)}</ul>
    </details>
    <details><summary>本轮工具与核对过程</summary>
      <pre style={{whiteSpace:"pre-wrap",overflowWrap:"anywhere"}}>{JSON.stringify(trace, null, 2)}</pre>
    </details>
  </section>;
}
