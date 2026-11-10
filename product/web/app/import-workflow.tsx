"use client";
import { FormEvent, useEffect, useState } from "react";
import { api, post, type Enterprise } from "./invoice-workflow";

type Target = {enterprise_id: string; invoice_id: string};
type Row = {row: number; invoice_id: string; accounting_period: string; purpose: string | null; note: string | null};
type Job = {
  job_id: string; file_name: string; state: "queued" | "ready" | "invalid" | "imported";
  preview: {rows: Row[]; errors: {row: number; message: string}[]} | null;
  imported_count: number | null;
};
const labels = {queued: "等待后台解析", ready: "待确认", invalid: "需修正文件", imported: "已导入"};

export default function ImportWorkflow({enterprises, inspect}: {
  enterprises: Enterprise[]; inspect: (target: Target) => void;
}) {
  const [enterprise, setEnterprise] = useState(enterprises[0].enterprise_id);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [job, setJob] = useState<Job | null>(null);
  const [busy, setBusy] = useState(false);
  const role = enterprises.find(item => item.enterprise_id === enterprise)?.role;
  const writable = role === "admin" || role === "editor";
  const [status, setStatus] = useState("");
  const path = "/api/enterprises/" + encodeURIComponent(enterprise) + "/imports";
  const pendingId = job?.state === "queued" ? job.job_id : null;

  useEffect(() => {
    if (!pendingId) return;
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const result: Job = await api(path + "/" + pendingId);
        if (!active) return;
        setJob(result);
        if (result.state !== "queued") return;
      } catch {
        if (!active) return;
        setStatus("暂时无法查询进度，可稍后查看导入记录。");
      }
      timer = setTimeout(poll, 1500);
    }
    timer = setTimeout(poll, 1000);
    return () => { active = false; clearTimeout(timer); };
  }, [path, pendingId]);

  async function run(action: () => Promise<void>) {
    setBusy(true);
    setStatus("");
    try { await action(); }
    catch (error) { setStatus(error instanceof Error ? error.message : "请求失败。"); }
    finally { setBusy(false); }
  }

  function upload(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    void run(async () => {
      setJob(await api(path, {method: "POST", body: form}));
      setJobs(await api(path));
    });
  }

  return <section>
    <h2>发票 Excel 导入</h2>
    <label>导入企业 <select value={enterprise} disabled={busy} onChange={event => {
      setEnterprise(event.target.value); setJob(null); setJobs([]); setStatus("");
    }}>{enterprises.map(e => <option key={e.enterprise_id} value={e.enterprise_id}>{e.name}</option>)}</select></label>{" "}
    <button disabled={busy} onClick={() => void run(async () => setJobs(await api(path)))}>查看导入记录</button>
    {writable && <form onSubmit={upload}>
      <p>上传单工作表 .xlsx，列顺序为 invoice_id、accounting_period、purpose、note。会计期间用 YYYY-MM 文本。</p>
      <label>Excel 文件 <input name="file" type="file" accept=".xlsx" required /></label>{" "}
      <button disabled={busy}>上传并解析</button>
    </form>}
    <ul>{jobs.map(entry => <li key={entry.job_id}>
      <button disabled={busy} onClick={() => void run(async () => setJob(await api(path + "/" + entry.job_id)))}>
        {entry.file_name} · {labels[job?.job_id === entry.job_id ? job.state : entry.state]}
      </button>
    </li>)}</ul>
    {job && <div>
      <p>{job.file_name} · {labels[job.state]} · 任务 {job.job_id}</p>
      {writable && job.state === "queued" && <button disabled={busy} onClick={() =>
        void run(async () => setJob(await api(path + "/" + job.job_id + "/retry", {method:"POST"})))
      }>重新排队</button>}
      {job.preview && <>
        <p>有效 {job.preview.rows.length} 行，错误 {job.preview.errors.length} 行。</p>
        {job.preview.errors.length > 0 && <ul>{job.preview.errors.map(error =>
          <li key={error.row}>第 {error.row} 行 · {error.message}</li>)}</ul>}
        <details open><summary>查看逐行预览</summary>
          <table><thead><tr><th>行号</th><th>发票</th><th>会计期间</th><th>用途</th><th>备注</th></tr></thead>
            <tbody>{job.preview.rows.map(row => <tr key={row.row}>
              <td>{row.row}</td><td>{row.invoice_id}</td><td>{row.accounting_period}</td>
              <td>{row.purpose ?? "空"}</td><td>{row.note ?? "空"}</td>
            </tr>)}</tbody></table>
        </details>
      </>}
      {writable && job.state === "ready" && <button disabled={busy} onClick={() => void run(async () => {
        try {
          setJob(await api(path + "/" + job.job_id + "/confirm", post({confirmed:true})));
        } catch (error) {
          if (error instanceof Error && error.message.includes("冲突")) throw new Error("存在同号发票，整批未写入。");
          throw error;
        }
      })}>确认导入 {job.preview?.rows.length} 张发票</button>}
      {job.state === "imported" && job.preview?.rows[0] && <>
        <p>已保存 {job.imported_count} 张发票。</p>
        <a href="#invoice-workflow" onClick={() => inspect({enterprise_id:enterprise, invoice_id:job.preview!.rows[0].invoice_id})}>核验首张已导入发票</a>
      </>}
    </div>}
    <p role="status">{status}</p>
  </section>;
}
