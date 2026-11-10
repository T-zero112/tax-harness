"use client";
import { FormEvent, useState } from "react";
import InvoiceWorkflow, {type Enterprise} from "./invoice-workflow";
import RuleWorkflow from "./rule-workflow";
import ImportWorkflow from "./import-workflow";

import MemberWorkflow from "./member-workflow";
import BankWorkflow from "./bank-workflow";

export default function Home() {
  const [enterprises, setEnterprises] = useState<Enterprise[]>([]);
  const [status, setStatus] = useState("");
  const [username, setUsername] = useState("");
  const [busy, setBusy] = useState(false);
  const [target, setTarget] = useState<{enterprise_id: string; invoice_id: string}>();

  async function login(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setBusy(true);
    setEnterprises([]);
    setTarget(undefined);
    try {
      const session = await fetch("/api/session", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({username: form.get("username"), password: form.get("password")}),
      });
      if (!session.ok) throw new Error("登录失败，请检查账户和密码。");
      const identity = await session.json();
      const [response, access] = await Promise.all([fetch("/api/enterprises"), fetch("/api/enterprise-access")]);
      if (!response.ok || !access.ok) throw new Error("企业权限读取失败。");
      const grants = new Map<string, Enterprise["role"]>((await access.json()).map((row: Enterprise) => [row.enterprise_id, row.role]));
      const items: Omit<Enterprise, "role">[] = await response.json();
      setEnterprises(items.flatMap(item => {
        const role = grants.get(item.enterprise_id);
        return role ? [{...item, role}] : [];
      }));
      setUsername(identity.username);
      setStatus(items.length ? "已登录" : "已登录，当前账户没有企业授权。");
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "请求失败");
    } finally {
      setBusy(false);
    }
  }

  async function logout() {
    setBusy(true);
    try {
      const response = await fetch("/api/session", {method:"DELETE"});
      if (!response.ok) throw new Error("退出失败，请重试。");
      setUsername("");
      setEnterprises([]);
      setTarget(undefined);
      setStatus("已退出");
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "退出失败。");
    } finally {
      setBusy(false);
    }
  }

  async function probe(enterprise: Enterprise) {
    setBusy(true);
    setStatus("正在检查连接");
    try {
      const response = await fetch("/api/enterprises/" + encodeURIComponent(enterprise.enterprise_id) + "/probe", {method:"POST"});
      if (!response.ok || !response.body) throw new Error("连接检查失败。");
      const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
      let buffer = "";
      let done = false;
      while (true) {
        const chunk = await reader.read();
        if (chunk.done) break;
        buffer += chunk.value;
        const frames = buffer.split("\n\n");
        buffer = frames.pop() ?? "";
        for (const frame of frames) {
          const data = frame.split("\n").find(line => line.startsWith("data: "));
          if (data) setStatus(data.slice(6));
          if (frame.startsWith("event: done\n")) done = true;
        }
      }
      if (!done) throw new Error("连接检查中断。");
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "请求失败");
    } finally {
      setBusy(false);
    }
  }

  return <main style={{maxWidth: 680, margin:"4rem auto", padding:"0 1rem", fontFamily:"system-ui"}}>
    <h1>财税资料核验工作台</h1>
    {username ? <p>当前账户 {username} <button disabled={busy} onClick={logout}>退出登录</button></p> :
    <form onSubmit={login} style={{display:"grid", gap:12, maxWidth:320}}>
      <label>账户 <input name="username" autoComplete="username" required /></label>
      <label>密码 <input name="password" type="password" autoComplete="current-password" required /></label>
      <button disabled={busy}>登录</button>
    </form>}
    <ul>{enterprises.map(enterprise => <li key={enterprise.enterprise_id}>
      {enterprise.name}（{enterprise.enterprise_id}） <button disabled={busy} onClick={() => probe(enterprise)}>检查 Agent 连接</button>
    </li>)}</ul>
    <p role="status">{status}</p>
    {enterprises.length > 0 && <ImportWorkflow enterprises={enterprises} inspect={setTarget} />}
    {enterprises.length > 0 && <InvoiceWorkflow key={target ? target.enterprise_id + "/" + target.invoice_id : "default"}
      enterprises={enterprises} target={target} />}
    {enterprises.length > 0 && <RuleWorkflow enterprises={enterprises} />}
    {enterprises.length > 0 && <BankWorkflow enterprises={enterprises} />}
    {enterprises.some(item => item.role === "admin") &&
      <MemberWorkflow enterprises={enterprises.filter(item => item.role === "admin")} />}
  </main>;
}
