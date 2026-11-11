"use client";
/* Gateway payloads are heterogeneous; display only returned values and keep raw data folded. */
/* eslint-disable @typescript-eslint/no-explicit-any */
export type Entry = { event: string; data: any };
export type Round = {
  turn_id: string;
  status: string;
  events: Entry[];
  steps?: number;
  context_bytes?: number;
  duration_ms?: number;
  summary?: any;
};
const names: Record<string, string> = {
  list_invoices: "发票查询",
  list_bank_transactions: "银行流水查询",
  search_rules: "适用规则检索",
  get_invoice: "发票详情",
  check_purpose: "发票用途核验",
  create_proposal: "补全建议",
  get_operation: "操作状态查询",
};
export function toolName(s: string) {
  return names[s] ?? s;
}
const guards: Record<string, string> = {
  amount_unverified: "金额未经工具核实，已拦截",
  proposal_target_unverified: "目标票号未确认，已拦截建议",
  scope_conflict: "企业范围冲突，请先切换企业",
  requirements_fixed: "需求清单已登记，不可更改",
  proposal_value_unverified: "建议字段缺少用户提供的原文",
  proposal_evidence_missing: "尚无保存或提案证据，已拦截相关声明",
  invalid_model_response: "模型决策格式无效，正在重试",
  unknown_tool: "模型请求了未开放的工具，已拦截",
  step_limit: "已达到本轮六步上限，任务未完成",
  answer_items_incomplete: "答案项不完整，仍缺少证据",
  evidence_mismatch: "证据与需求不匹配",
  direction_mismatch: "银行收支方向不符",
  filter_mismatch: "查询条件与证据不符",
  invoice_amount_unknown: "部分发票金额未知，不能报告完整合计",
  list_incomplete: "返回明细尚不完整",
  evidence_conflict: "工具证据存在冲突",
  requirements_missing: "未登记可核实的需求",
  duplicate_requirements: "需求编号重复",
  invalid_arguments: "工具参数不符合契约",
  empty_reply: "模型没有提供有效回答",
  context_budget_exceeded: "必要上下文超出预算，待办已保留",
  model_unavailable: "模型请求失败或超时，待办已保留",
  tool_unavailable: "业务调用结果待核实",
  turn_failed: "本轮连接或执行失败，请核实结果",
  trace_unavailable: "轨迹未确认落库，请恢复任务核实",
};
export function guardText(s: string) {
  return guards[s] ?? "本轮受到保护性拦截，请核对范围与输入（" + s + "）。";
}
const types: Record<string, string> = {
  invoice_total: "发票合计",
  income_total: "银行收入",
  expense_total: "银行支出",
  rules: "适用规则",
  invoices: "发票明细",
  review: "用途核验",
  operation: "操作状态",
};
function money(value: unknown) {
  try {
    const n = BigInt(String(value));
    return (
      (n < BigInt(0) ? "-" : "") +
      (n < BigInt(0) ? -n : n) / BigInt(100) +
      "." +
      ((n < BigInt(0) ? -n : n) % BigInt(100)).toString().padStart(2, "0") +
      " 元"
    );
  } catch {
    return "金额格式待核实";
  }
}
export default function ProcessPanel({
  rounds,
  selected,
}: {
  rounds: Round[];
  selected: string;
}) {
  return (
    <div>
      {!rounds.length && (
        <p className="empty">
          发送问题后，这里会显示需求、工具证据和程序拦截。恢复的任务可点击“查看完整轨迹”。
        </p>
      )}
      {rounds.map((r, index) => {
        const events = r.events ?? [],
          summary = r.summary,
          decisions =
            summary?.decisions ??
            events.filter((e) => e.event === "decision").map((e) => e.data),
          requirements =
            events.filter((e) => e.event === "requirements").at(-1)?.data
              .items ?? [];
        const results = events.filter((e) => e.event === "tool_result"),
          answerItems =
            summary?.decisions?.filter((d: any) => d.kind === "answer").at(-1)
              ?.answer_items ?? [];
        const snapshot = summary?.context_snapshots?.at(-1),
          used = snapshot?.memory_refs ?? [];
        return (
          <section
            className="round"
            key={r.turn_id}
            aria-label={"执行轮次 " + (index + 1)}
          >
            <h3>
              第 {index + 1} 轮{" "}
              <span className="tag">
                {(
                  {
                    succeeded: "已回答",
                    needs_input: "需要补充",
                    pending_confirmation: "待确认",
                    incomplete: "未完成",
                    running: "执行中",
                  } as Record<string, string>
                )[r.status] ?? "未完成"}
              </span>
            </h3>
            <details>
              <summary>轮次编号</summary>
              <code>{r.turn_id}</code>
            </details>
            <h3>需求清单</h3>
            {requirements.length ? (
              <ul className="requirements">
                {requirements.map((q: any) => {
                  const matched = answerItems.find(
                    (a: any) => a.requirement_id === q.requirement_id,
                  );
                  const ids = new Set(
                    results
                      .filter((e) => e.data.result?.status === "succeeded")
                      .map((e) => e.data.evidence_id),
                  );
                  const checked =
                    matched?.evidence_ids?.length &&
                    matched.evidence_ids.every((id: string) => ids.has(id)) &&
                    r.status === "succeeded";
                  return (
                    <li key={q.requirement_id}>
                      <strong>{types[q.kind] ?? q.kind}</strong>{" "}
                      <span className={"tag " + (checked ? "good" : "warn")}>
                        {checked ? "已核实" : "缺证据 / 待核对"}
                      </span>
                      <small> {q.requirement_id}</small>
                      {Object.keys(q.filters ?? {}).length > 0 && (
                        <pre>{JSON.stringify(q.filters, null, 2)}</pre>
                      )}
                    </li>
                  );
                })}
              </ul>
            ) : (
              <p className="muted">尚未登记需求。</p>
            )}
            <h3>步骤时间线</h3>
            <ol className="timeline">
              {decisions.map((d: any, i: number) => (
                <li key={i}>
                  第 {i + 1}/6 步 ·{" "}
                  {(
                    {
                      tool: "查询工具",
                      ask: "追问",
                      answer: "核对答案",
                      propose: "生成建议",
                      invalid: "无效决策",
                    } as Record<string, string>
                  )[d.kind] ?? d.kind}
                  {d.tool && " · " + toolName(d.tool)}
                </li>
              ))}
            </ol>
            {events
              .filter(
                (e) =>
                  (e.event === "status" || e.event === "error") && e.data.code,
              )
              .map((e, i) => (
                <div className="notice warn" key={i}>
                  <strong>程序保护</strong>
                  <p>{guardText(e.data.code)}</p>
                  <span className="error-code">{e.data.code}</span>
                </div>
              ))}
            {results.map((e, i) => {
              const d = e.data,
                data = d.result?.data ?? {},
                call = events.filter(
                  (x) => x.event === "tool_call" && x.data.tool === d.tool,
                )[
                  results.slice(0, i).filter((x) => x.data.tool === d.tool)
                    .length
                ],
                id = d.evidence_id;
              return (
                <article
                  className={
                    "tool-card " + (id && selected === id ? "selected" : "")
                  }
                  key={id ?? i}
                  id={id ? "evidence-" + id : undefined}
                  tabIndex={-1}
                >
                  <h3>{toolName(d.tool)}</h3>
                  <p>
                    {d.result?.status === "succeeded"
                      ? "工具返回成功"
                      : "工具未成功，不能作为答案依据"}
                    {d.duration_ms !== undefined &&
                      " · " + d.duration_ms + " ms"}
                  </p>
                  {call && <p>参数：{JSON.stringify(call.data.arguments)}</p>}
                  {data.records && (
                    <p>
                      展示明细 {data.records.length} 条
                      {data.summary?.count !== undefined &&
                        " · 全部匹配 " + data.summary.count + " 条"}
                      {data.has_more && " · 还有更多明细"}
                    </p>
                  )}
                  {Object.entries(data.summary ?? {})
                    .filter(([k]) => k.endsWith("_cents"))
                    .map(([k, v]) => (
                      <p key={k}>
                        {(
                          {
                            invoice_cents: "发票合计",
                            income_cents: "银行收入",
                            expense_cents: "银行支出",
                          } as Record<string, string>
                        )[k] ?? k}
                        ：{money(v)}
                        {data.summary?.unknown_amounts > 0 &&
                          "（金额存在缺失，不完整）"}
                      </p>
                    ))}
                  {data.rules?.map((rule: any, j: number) => (
                    <p key={j}>
                      {rule.citation?.source ?? rule.source} · 版本{" "}
                      {rule.citation?.version ?? rule.version} · 条款{" "}
                      {rule.citation?.clause ?? rule.clause}
                    </p>
                  ))}
                  {id && (
                    <p>
                      <small>证据编号 {id}</small>
                    </p>
                  )}
                  <details>
                    <summary>工具原始 JSON</summary>
                    <pre>{JSON.stringify(d, null, 2)}</pre>
                  </details>
                </article>
              );
            })}
            {summary && (
              <div>
                <h3>上下文信息</h3>
                <p>
                  实际输入{" "}
                  {r.context_bytes ?? summary.context_bytes ?? "未返回"} 字节
                  {summary.context_budget !== undefined &&
                    " / 预算 " + summary.context_budget + " 字节"}
                  {r.duration_ms !== undefined &&
                    " · 本轮 " + r.duration_ms + " ms"}
                </p>
                <p>裁剪 {summary.trimming?.length ?? "未返回"} 项</p>
                {summary.trimming?.length > 0 && (
                  <pre>{JSON.stringify(summary.trimming, null, 2)}</pre>
                )}
                <p>使用偏好 {snapshot ? used.length : "未返回"} 条</p>
                {used.map((m: any) => (
                  <p key={m.memory_id}>
                    <small>
                      {m.memory_id} · 版本 {m.revision}
                    </small>
                  </p>
                ))}
                <details>
                  <summary>开发者详情（脱敏）</summary>
                  <p>仅供定位问题，以下为已脱敏的模型输入与决策。</p>
                  <pre>
                    {JSON.stringify(
                      {
                        context_snapshots: summary.context_snapshots,
                        decisions: summary.decisions,
                      },
                      null,
                      2,
                    )}
                  </pre>
                </details>
              </div>
            )}
          </section>
        );
      })}
    </div>
  );
}
