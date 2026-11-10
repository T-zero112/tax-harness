"use client";
import {FormEvent, useState} from "react";
import {api, type Enterprise} from "./invoice-workflow";

type Row = {transaction_id: string; transaction_date: string; direction: "income" | "expense";
  amount_cents: string; counterparty: string; note: string | null};
type Page = {enterprise_id: string; accounting_period: string; records: Row[]; has_more: boolean};

function yuan(cents: string) {
  const digits = cents.padStart(3, "0");
  return digits.slice(0, -2) + "." + digits.slice(-2);
}

export default function BankWorkflow({enterprises}: {enterprises: Enterprise[]}) {
  const [result, setResult] = useState<Page | null>(null);
  const [query, setQuery] = useState<{path: string; offset: number} | null>(null);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");

  async function load(path: string, offset: number) {
    setBusy(true);
    setStatus("");
    setResult(null);
    setQuery(null);
    try {
      setResult(await api(path + "&offset=" + offset));
      setQuery({path, offset});
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "流水查询失败。");
    } finally {
      setBusy(false);
    }
  }

  function search(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const enterprise = encodeURIComponent(String(form.get("enterprise")));
    const params = new URLSearchParams({period:String(form.get("period"))});
    const id = String(form.get("transaction_id") ?? "").trim();
    if (id) params.set("transaction_id", id);
    void load("/api/enterprises/" + enterprise + "/bank-transactions?" + params, 0);
  }

  return <section>
    <h2>银行流水查询</h2>
    <form onSubmit={search}>
      <label>流水企业 <select name="enterprise">{enterprises.map(item =>
        <option key={item.enterprise_id} value={item.enterprise_id}>{item.name}</option>)}</select></label>{" "}
      <label>流水会计期间 <input name="period" type="month" required /></label>{" "}
      <label>流水号 <input name="transaction_id" maxLength={64} placeholder="可留空" /></label>{" "}
      <button disabled={busy}>查询流水</button>
    </form>
    {result && <div>
      <p>{result.enterprise_id} · {result.accounting_period} · 只读</p>
      {result.records.length ? <div style={{overflowX:"auto"}}><table>
        <thead><tr><th>流水号</th><th>交易日期</th><th>方向</th><th>金额（元）</th><th>交易对方</th><th>备注</th></tr></thead>
        <tbody>{result.records.map(row => <tr key={row.transaction_id}>
          <td>{row.transaction_id}</td><td>{row.transaction_date}</td><td>{row.direction === "income" ? "收入" : "支出"}</td>
          <td>{yuan(row.amount_cents)}</td><td>{row.counterparty}</td><td>{row.note ?? "空"}</td>
        </tr>)}</tbody>
      </table></div> : <p>当前条件下没有流水。</p>}
      {query && query.offset > 0 && <button disabled={busy} onClick={() => void load(query.path, query.offset - 100)}>上一页</button>}
      {query && result.has_more && <button disabled={busy} onClick={() => void load(query.path, query.offset + 100)}>下一页</button>}
    </div>}
    <p role="status">{busy ? "正在查询流水" : status}</p>
  </section>;
}
