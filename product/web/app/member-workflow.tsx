"use client";
import { FormEvent, useState } from "react";
import { api, post, type Enterprise } from "./invoice-workflow";

type Member = {username: string; role: Enterprise["role"]};
const roles = {admin:"管理员", editor:"经办人", viewer:"查看者"};

export default function MemberWorkflow({enterprises}: {enterprises: Enterprise[]}) {
  const [enterprise, setEnterprise] = useState(enterprises[0].enterprise_id);
  const [members, setMembers] = useState<Member[]>([]);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");
  const path = "/api/enterprises/" + encodeURIComponent(enterprise) + "/members";

  async function run(action?: () => Promise<unknown>) {
    setBusy(true);
    setStatus("");
    try {
      if (action) await action();
      setMembers(await api(path));
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "请求失败。");
    } finally {
      setBusy(false);
    }
  }

  function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const values = Object.fromEntries(new FormData(form));
    void run(async () => {
      await api(path, post(values));
      form.reset();
      setStatus("账户已创建，并获得当前企业的访问权限。");
    });
  }

  function grant(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const username = String(form.get("existing"));
    void run(async () => {
      await api(path + "/" + encodeURIComponent(username), {...post({role:form.get("role")}), method:"PUT"});
      setStatus("已有账户获得当前企业的访问权限。");
    });
  }

  return <section>
    <h2>企业成员与权限</h2>
    <label>授权企业 <select value={enterprise} disabled={busy} onChange={event => {
      setEnterprise(event.target.value); setMembers([]); setStatus("");
    }}>{enterprises.map(item => <option key={item.enterprise_id} value={item.enterprise_id}>{item.name}</option>)}</select></label>{" "}
    <button disabled={busy} onClick={() => void run()}>查看成员</button>
    <form onSubmit={create} style={{display:"grid",gap:8,maxWidth:400}}>
      <label>新账户 <input name="username" required minLength={3} maxLength={64} /></label>
      <label>初始密码 <input name="password" type="password" minLength={12} maxLength={256} autoComplete="new-password" required /></label>
      <label>企业角色 <select name="role"><option value="viewer">查看者</option><option value="editor">经办人</option></select></label>
      <button disabled={busy}>创建并授权</button>
    </form>
    <form onSubmit={grant}>
      <label>已有账户 <input name="existing" required /></label>{" "}
      <label>授权角色 <select name="role"><option value="viewer">查看者</option><option value="editor">经办人</option></select></label>{" "}
      <button disabled={busy}>授予企业权限</button>
    </form>
    <ul>{members.map(member => <li key={member.username}>
      {member.username} · {roles[member.role]}{" "}
      {member.role !== "admin" && <>
        <button disabled={busy} onClick={() => void run(() => api(path + "/" + encodeURIComponent(member.username), {
          ...post({role: member.role === "editor" ? "viewer" : "editor"}), method:"PATCH",
        }))}>{member.role === "editor" ? "设为查看者" : "设为经办人"}</button>{" "}
        <button disabled={busy} onClick={() => void run(() => api(path + "/" + encodeURIComponent(member.username),
          {method:"DELETE"}))}>撤销企业权限</button>
      </>}
    </li>)}</ul>
    <p role="status">{status}</p>
  </section>;
}
