"use client";
import { FormEvent, useState } from "react";
import { api, post, type Enterprise, type RuleRef } from "./invoice-workflow";

type Rule = {citation: RuleRef; content: string; state: string; effective_from: string; effective_until: string | null};
type Hit = {rule: Rule; semantic_score: number; exact_match: boolean};

export default function RuleWorkflow({enterprises}: {
  enterprises: Enterprise[];
}) {
  const [enterprise, setEnterprise] = useState(enterprises[0].enterprise_id);
  const [rules, setRules] = useState<Rule[]>([]);
  const [hits, setHits] = useState<Hit[] | null>(null);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const role = enterprises.find(item => item.enterprise_id === enterprise)?.role;
  const writable = role === "admin";
  const path = "/api/enterprises/" + encodeURIComponent(enterprise) + "/rules";
  const refresh = async () => setRules(await api(path));

  async function run(action: () => Promise<void>) {
    setBusy(true);
    setMessage("");
    try { await action(); }
    catch (error) { setMessage(error instanceof Error ? error.message : "请求失败。"); }
    finally { setBusy(false); }
  }

  function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    void run(async () => {
      await api(path, post({...Object.fromEntries(form), effective_until: form.get("effective_until") || null,
        required_fields: form.getAll("required_fields")}));
      await refresh();
      setHits(null);
      setMessage("规则草稿已保存。");
    });
  }

  function search(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    void run(async () => setHits(await api(path + "/search?" + new URLSearchParams({
      period: String(form.get("period")), query: String(form.get("query")),
    }))));
  }

  return <section>
    <h2>规则版本与检索</h2>
    <label>规则企业 <select value={enterprise} disabled={busy} onChange={event => {
      setEnterprise(event.target.value); setRules([]); setHits(null); setMessage("");
    }}>{enterprises.map(e => <option key={e.enterprise_id} value={e.enterprise_id}>{e.name}</option>)}</select></label>{" "}
    <button disabled={busy} onClick={() => void run(refresh)}>查看规则版本</button>
    <ul>{rules.map(rule => <li key={rule.citation.rule_id}>
      {rule.citation.source} · {rule.citation.version} · {rule.citation.clause} · {rule.state} ·
      {rule.effective_from} 至 {rule.effective_until ?? "长期"}
      {writable && rule.state !== "retired" && <button disabled={busy} onClick={() => {
        const action = rule.state === "draft" ? "publish" : "retire";
        void run(async () => { await api(path + "/" + rule.citation.rule_id + "/" + action, {method:"POST"}); await refresh(); setHits(null); });
      }}>{rule.state === "draft" ? "发布" : "退役"}</button>}
    </li>)}</ul>
    {writable && <details><summary>新增规则版本</summary>
      <form onSubmit={create} style={{display:"grid", gap:8}}>
        <label>规则编号 <input name="code" required maxLength={80} /></label>
        <label>版本 <input name="version" required maxLength={40} /></label>
        <label>来源 <input name="source" required maxLength={120} /></label>
        <label>条款 <input name="clause" required maxLength={80} /></label>
        <label>正文 <textarea name="content" required maxLength={600} /></label>
        <label>生效月份 <input type="month" name="effective_from" required /></label>
        <label>失效月份（该月起不适用） <input type="month" name="effective_until" /></label>
        <label><input type="checkbox" name="required_fields" value="purpose" defaultChecked />用途必填</label>
        <label><input type="checkbox" name="required_fields" value="note" defaultChecked />备注必填</label>
        <button disabled={busy}>保存草稿</button>
      </form>
    </details>}
    <form onSubmit={search}>
      <label>会计期间 <input type="month" name="period" defaultValue="2026-09" required /></label>{" "}
      <label>规则查询 <input name="query" maxLength={240} required /></label>{" "}
      <button disabled={busy}>检索规则</button>
    </form>
    {hits && (hits.length ? <ol>{hits.map(hit => <li key={hit.rule.citation.rule_id}>
      {hit.rule.citation.source} · {hit.rule.citation.version} · {hit.rule.citation.clause}
      <p>{hit.rule.content}（{hit.exact_match ? "字面匹配" : "语义匹配"}，余弦分数 {hit.semantic_score.toFixed(3)}）</p>
    </li>)}</ol> : <p>当前期间无适用规则。</p>)}
    <p role="status">{message}</p>
  </section>;
}
