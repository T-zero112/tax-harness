"use client";
import { FormEvent, useEffect, useRef, useState } from "react";
import { api, post, type Enterprise, type RuleRef } from "./invoice-workflow";
import ProcessPanel, {
  type Entry,
  type Round,
  toolName,
  guardText,
} from "./process-panel";
type Proposal = {
  invoice_id: string;
  before: { purpose: string | null; note: string | null; revision: number };
  proposal: {
    proposal_id: string;
    expected_revision: number;
    changes: Record<string, string>;
    rule_ref: RuleRef;
  };
};
type Task = {
  task_id: string;
  revision: number;
  accounting_period: string;
  status: string;
  object_id: string | null;
  question: string | null;
  bound_scope_id: string;
};
type Message = {
  message_id: string;
  role: string;
  content: string;
  evidence?: Entry[];
};
type Memory = {
  memory_id: string;
  content: string;
  revision: number;
  source_message_id: string;
  enterprise_id: string | null;
};
type Operation = {
  operation_id: string;
  kind: string;
  state: string;
  idempotency_key: string | null;
};
const statuses: Record<string, string> = {
  succeeded: "已回答",
  needs_input: "需要你补充",
  pending_confirmation: "待你确认",
  incomplete: "未完成",
  waiting_user: "等待补充或确认",
  running: "执行中",
  blocked: "待核实",
  cancelled: "已取消",
};
export default function ChatWorkflow({
  enterprises,
  onScope,
  onModel,
}: {
  enterprises: Enterprise[];
  onScope?: (s: string) => void;
  onModel?: (s: string) => void;
}) {
  const [enterprise, setEnterprise] = useState(enterprises[0].enterprise_id),
    [period, setPeriod] = useState("2026-09"),
    [session, setSession] = useState("");
  const [message, setMessage] = useState(""),
    [busy, setBusy] = useState(false),
    [status, setStatus] = useState(""),
    [final, setFinal] = useState("");
  const [messages, setMessages] = useState<Message[]>([]),
    [rounds, setRounds] = useState<Round[]>([]),
    [selected, setSelected] = useState("");
  const [proposal, setProposal] = useState<Proposal | null>(null),
    [collapsed, setCollapsed] = useState(false),
    [task, setTask] = useState<Task | null>(null);
  const [matches, setMatches] = useState(true),
    [resumed, setResumed] = useState(false),
    [memories, setMemories] = useState<Memory[]>([]),
    [operations, setOperations] = useState<Operation[]>([]);
  const [memoryContent, setMemoryContent] = useState(""),
    [source, setSource] = useState(""),
    [wide, setWide] = useState(false),
    [audit, setAudit] = useState<unknown[]>([]);
  const [tab, setTab] = useState("chat"),
    [cancelling, setCancelling] = useState(false),
    [correctTarget, setCorrectTarget] = useState(""),
    [question, setQuestion] = useState("");
  const [savePhase, setSavePhase] = useState(""),
    [uncertain, setUncertain] = useState(false);
  const key = useRef(""),
    keyProposal = useRef(""),
    inFlight = useRef(false),
    currentTask = useRef(""),
    bottom = useRef<HTMLDivElement>(null);
  const confirmation = useRef<{
    invoice: string;
    proposal: string;
    task: string;
  } | null>(null);
  const base = "/api/enterprises/" + encodeURIComponent(enterprise);
  useEffect(() => {
    setSession(crypto.randomUUID());
  }, []);
  useEffect(() => {
    onScope?.(
      (enterprises.find((e) => e.enterprise_id === enterprise)?.name ??
        enterprise) +
        " · " +
        period,
    );
  }, [enterprise, period]);
  useEffect(() => {
    if (session)
      refresh(enterprise, period, true).catch((e) => setStatus(e.message));
  }, [session]);
  useEffect(() => {
    bottom.current?.scrollIntoView({ block: "nearest" });
  }, [messages.length]);
  async function refresh(e = enterprise, p = period, restore = false) {
    const path = "/api/enterprises/" + encodeURIComponent(e) + "/chat";
    const [active, prefs] = await Promise.all([
      api(path + "/tasks/active?period=" + p),
      api(path + "/memories"),
    ]);
    setTask(active.task);
    setMemories(prefs);
    setOperations(active.unfinished_operations ?? []);
    setMatches(active.matches_period ?? true);
    setResumed(
      !!active.task &&
        !active.requires_resume &&
        active.task.bound_scope_id === session,
    );
    setProposal(active.proposal ?? null);
    currentTask.current = active.task?.task_id ?? currentTask.current;
    if (restore) setMessages(active.messages ?? []);
    else if (active.messages?.length)
      setMessages((old) => {
        const known = new Set(old.map((m) => m.message_id));
        return [
          ...old,
          ...active.messages.filter(
            (m: Message) =>
              !known.has(m.message_id) &&
              !old.some((x) => x.content === m.content && x.role === m.role),
          ),
        ];
      });
    const pending = (active.unfinished_operations ?? []).find(
      (op: Operation) => op.kind === "confirmation",
    );
    if (pending?.idempotency_key) key.current = pending.idempotency_key;
    else if (
      active.proposal &&
      keyProposal.current !== active.proposal.proposal.proposal_id
    ) {
      key.current = crypto.randomUUID();
      keyProposal.current = active.proposal.proposal.proposal_id;
    }
    if (active.task?.question) setStatus(active.task.question);
    return active;
  }
  async function failure(error: unknown) {
    const text =
      error instanceof Error ? error.message : "连接中断，请核实结果。";
    setStatus(text);
    if (text.includes("连接中断")) setFinal("incomplete");
    if (text.includes("冲突")) await refresh().catch(() => {});
    setStatus(text);
  }
  async function run(action: () => Promise<void>) {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    try {
      await action();
    } catch (e) {
      await failure(e);
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }
  async function loadTraces(id = currentTask.current) {
    if (!id) return;
    const rows = await api(base + "/chat/tasks/" + id + "/traces");
    setRounds(
      rows.map((r: Round) => ({ ...r, events: r.summary?.events ?? [] })),
    );
  }
  async function resume() {
    await run(async () => {
      if (!task) return;
      await api(
        base + "/chat/tasks/" + task.task_id + "/resume",
        post({ session_id: session, period, expected_revision: task.revision }),
      );
      await refresh();
      setCollapsed(false);
      setStatus("任务已恢复，请核对建议后确认保存。");
    });
  }
  async function cancel() {
    await run(async () => {
      if (!task) return;
      await api(
        base + "/chat/tasks/" + task.task_id + "/cancel",
        post({ expected_revision: task.revision }),
      );
      await refresh();
      setCancelling(false);
      setStatus("任务已取消。");
    });
  }
  async function correct(e: FormEvent) {
    e.preventDefault();
    await run(async () => {
      if (!task) return;
      await api(
        base + "/chat/tasks/" + task.task_id + "/correct",
        post({
          expected_revision: task.revision,
          ...(correctTarget ? { object_id: correctTarget } : {}),
          ...(question ? { question } : {}),
        }),
      );
      await refresh();
      setStatus("任务已更正，原建议已失效。");
    });
  }
  async function reconcile(op?: Operation) {
    await run(async () => {
      const active = await refresh();
      const list = op ? [op] : (active.unfinished_operations ?? []);
      if (!list.length) {
        if (confirmation.current) {
          const attempt = confirmation.current;
          const path =
            base + "/invoices/" + encodeURIComponent(attempt.invoice);
          const receipts = await api(path + "/audit");
          const saved = receipts.find(
            (row: { proposal_id: string }) =>
              row.proposal_id === attempt.proposal,
          );
          if (saved?.operation_id) {
            const result = await api(
              base +
                "/chat/tasks/" +
                attempt.task +
                "/operations/" +
                saved.operation_id +
                "/reconcile",
              post({}),
            );
            if (result.state === "confirmed") {
              const current = await api(path);
              setUncertain(false);
              setSavePhase(
                "已核实保存 · 操作编号 " +
                  saved.operation_id +
                  " · 保存版本 " +
                  saved.after.revision +
                  " · 当前回查版本 " +
                  current.revision,
              );
              setStatus("保存结果已核实。");
              return;
            }
          }
        }
        setStatus("未发现未结束操作，请查看发票及审计确认结果。");
        return;
      }
      for (const item of list)
        await api(
          base +
            "/chat/tasks/" +
            active.task.task_id +
            "/operations/" +
            item.operation_id +
            "/reconcile",
          post({}),
        );
      await refresh();
      setUncertain(false);
      setStatus("操作已核实，请核对业务记录。");
    });
  }
  async function saveMemory(existing?: Memory, deleting = false) {
    await run(async () => {
      const path =
        base +
        "/chat/memories" +
        (existing
          ? "/" + existing.memory_id + (deleting ? "" : "/correct")
          : "");
      await api(path, {
        ...post(
          existing
            ? { content: memoryContent, expected_revision: existing.revision }
            : {
                content: memoryContent,
                source_message_id: source,
                user_wide: wide,
              },
        ),
        method: deleting ? "DELETE" : "POST",
      });
      setSource("");
      setMemoryContent("");
      await refresh();
      setStatus(deleting ? "偏好已删除。" : "偏好已保存。");
    });
  }
  async function switchScope(e: string, p: string) {
    await run(async () => {
      await api(
        "/api/enterprises/" + encodeURIComponent(e) + "/chat/scope",
        post({ session_id: session, period: p }),
      );
      setProposal(null);
      setTask(null);
      setResumed(false);
      setMatches(false);
      setOperations([]);
      currentTask.current = "";
      setAudit([]);
      setSource("");
      setMemoryContent("");
      setSavePhase("");
      setUncertain(false);
      confirmation.current = null;
      setEnterprise(e);
      setPeriod(p);
      setMessages([]);
      setRounds([]);
      setFinal("");
      key.current = "";
      keyProposal.current = "";
      setCollapsed(false);
      setStatus("范围已切换。");
      await refresh(e, p, true);
    });
  }
  async function send(e: FormEvent) {
    e.preventDefault();
    if (!message.trim() || message.length > 4000) return;
    await run(async () => {
      const text = message,
        local = crypto.randomUUID();
      setMessages((old) => [
        ...old,
        { message_id: local, role: "user", content: text },
      ]);
      setFinal("");
      if (!uncertain) setSavePhase("");
      setStatus("正在处理本轮请求…");
      setCollapsed(false);
      const response = await fetch(
        base + "/chat",
        post({
          session_id: session,
          task_id: task?.task_id,
          period,
          message: text,
        }),
      );
      if (response.status === 503) {
        onModel?.("模型未配置");
        throw new Error("模型未配置，请配置后重试；业务数据页仍可使用。");
      }
      if (response.status === 401) {
        window.dispatchEvent(new Event("session-expired"));
        throw new Error("会话已过期，请重新登录。");
      }
      if (!response.ok || !response.body)
        throw new Error(
          (
            {
              403: "当前账户没有操作权限，请联系管理员。",
              404: "当前企业没有找到记录，请检查范围。",
              409: "任务状态或版本冲突，已刷新，请核对后重试。",
            } as Record<number, string>
          )[response.status] ?? "连接中断，请核实结果。",
        );
      onModel?.("模型已配置");
      setMessage("");
      let events: Entry[] = [],
        turn = local,
        id = task?.task_id ?? "",
        completed = false,
        buffer = "";
      const reader = response.body
        .pipeThrough(new TextDecoderStream())
        .getReader();
      setRounds((old) => [
        ...old,
        { turn_id: local, status: "running", events: [] },
      ]);
      while (true) {
        const chunk = await reader.read();
        if (chunk.done) break;
        buffer += chunk.value.replace(/\r\n/g, "\n");
        const frames = buffer.split("\n\n");
        buffer = frames.pop() ?? "";
        for (const frame of frames) {
          const name = frame
              .split("\n")
              .find((x) => x.startsWith("event: "))
              ?.slice(7),
            raw = frame
              .split("\n")
              .filter((x) => x.startsWith("data: "))
              .map((x) => x.slice(6))
              .join("\n");
          if (!name || !raw) continue;
          const data = JSON.parse(raw);
          events = [...events, { event: name, data }];
          if (name === "status" && data.task_id) {
            id = data.task_id;
            turn = data.turn_id;
            currentTask.current = id;
            setMessages((old) =>
              old.map((m) =>
                m.message_id === local
                  ? { ...m, message_id: data.message_id }
                  : m,
              ),
            );
          }
          if (name === "status" || name === "error")
            setStatus(
              data.code
                ? guardText(data.code)
                : (data.message ?? "正在核对证据…"),
            );
          if (name === "tool_call")
            setStatus("正在查询 / 核对：" + toolName(data.tool));
          if (name === "answer")
            setMessages((old) => [
              ...old,
              {
                message_id: crypto.randomUUID(),
                role: "assistant",
                content: data.message,
                evidence: events.filter(
                  (x) => x.event === "tool_result" && x.data.evidence_id,
                ),
              },
            ]);
          if (name === "proposal") {
            setProposal(data);
            key.current = crypto.randomUUID();
            keyProposal.current = data.proposal.proposal_id;
            setStatus("建议已生成，请核对后确认。");
          }
          if (name === "done") {
            completed = true;
            setFinal(statuses[data.status] ? data.status : "incomplete");
          }
          setRounds((old) =>
            old.map((r) =>
              r.turn_id === local || r.turn_id === turn
                ? {
                    ...r,
                    turn_id: turn,
                    status: name === "done" ? data.status : r.status,
                    events,
                  }
                : r,
            ),
          );
        }
      }
      if (!completed) {
        setFinal("incomplete");
        throw new Error("连接中断，请核实结果。缩小问题后重试。");
      }
      await refresh();
      await loadTraces(id);
      const last = events.filter((entry) => entry.event === "done").at(-1)
        ?.data.status;
      if (last === "succeeded") setStatus("本轮已回答，结果与依据已列出。");
      else if (last === "pending_confirmation")
        setStatus("建议待你确认，请核对字段与规则。");
      else if (last === "incomplete") {
        const code = events.filter((entry) => entry.event === "error").at(-1)
          ?.data.code;
        setStatus(code ? guardText(code) : "本轮未完成，请核实结果。");
      }
    });
  }
  async function confirm() {
    await run(async () => {
      if (!proposal || !matches || !resumed || operations.length || uncertain)
        return;
      const target =
        base + "/invoices/" + encodeURIComponent(proposal.invoice_id);
      confirmation.current = {
        invoice: proposal.invoice_id,
        proposal: proposal.proposal.proposal_id,
        task: task!.task_id,
      };
      setSavePhase("保存中");
      try {
        const receipt = await api(
          target + "/proposals/" + proposal.proposal.proposal_id + "/confirm",
          post(
            { confirmed: true },
            { "Idempotency-Key": key.current, "X-Chat-Scope": session },
          ),
        );
        setSavePhase("回查中");
        const current = await api(target);
        if (
          current.revision !== receipt.after.revision ||
          Object.keys(proposal.proposal.changes).some(
            (f) => current[f] !== receipt.after[f],
          )
        )
          throw new Error("回查存在差异。");
        const text =
          "已保存 · 操作编号 " +
          (receipt.operation_id ?? receipt.idempotency_key) +
          " · 回查版本 " +
          current.revision;
        setMessages((old) => [
          ...old,
          { message_id: crypto.randomUUID(), role: "system", content: text },
        ]);
        setSavePhase(text);
        setUncertain(false);
        await refresh();
        setStatus("保存与回查通过。");
      } catch (e) {
        setSavePhase("保存结果待核实");
        setUncertain(true);
        await refresh().catch(() => {});
        throw new Error(
          "保存结果待核实。请核实操作结果，再决定是否重试；重试复用原幂等键。",
        );
      }
    });
  }
  function focusEvidence(id: string) {
    setSelected(id);
    setTab("process");
    requestAnimationFrame(() =>
      document
        .getElementById("evidence-" + id)
        ?.scrollIntoView({ block: "center" }),
    );
  }
  const reason = uncertain
    ? "保存结果待核实，请先核实操作结果。"
    : !matches
      ? "跨月不可确认，请切回任务月份。"
      : operations.length
        ? "存在未核实操作，请先核实结果。"
        : !resumed
          ? "需先恢复任务，重新核对建议。"
          : busy
            ? "当前操作进行中。"
            : "";
  return (
    <>
      <div className="mobile-tabs" role="group" aria-label="工作台视图">
        {[
          ["chat", "对话"],
          ["process", "过程"],
          ["task", "任务"],
        ].map(([k, v]) => (
          <button key={k} aria-pressed={tab === k} onClick={() => setTab(k)}>
            {v}
          </button>
        ))}
      </div>
      <section className="workspace" data-tab={tab} aria-label="对话工作台">
        <aside className="scope-panel" aria-label="任务与偏好">
          <span className="eyebrow">工作范围</span>
          <h2>企业与任务</h2>
          <label>
            对话企业
            <select
              value={enterprise}
              disabled={busy}
              onChange={(e) => switchScope(e.target.value, period)}
            >
              {enterprises.map((e) => (
                <option key={e.enterprise_id} value={e.enterprise_id}>
                  {e.name}
                </option>
              ))}
            </select>
          </label>
          <label>
            对话月份
            <input
              type="month"
              value={period}
              disabled={busy}
              onChange={(e) => {
                if (e.target.value) switchScope(enterprise, e.target.value);
              }}
            />
          </label>
          <article className="task-card">
            <h3>当前任务</h3>
            {task ? (
              <>
                <span className="tag">
                  {statuses[task.status] ?? task.status}
                </span>
                <dl>
                  <dt>目标票号</dt>
                  <dd>{task.object_id ?? "尚未明确"}</dd>
                  <dt>会计期间</dt>
                  <dd>{task.accounting_period}</dd>
                  <dt>版本</dt>
                  <dd>{task.revision}</dd>
                </dl>
                <p>{task.question ?? "暂无待补充问题"}</p>
                <details>
                  <summary>任务编号</summary>
                  <code>{task.task_id}</code>
                </details>
                {!matches && (
                  <p className="notice warn">
                    当前月份仅能查询。原任务仍属于 {task.accounting_period}。
                  </p>
                )}
                <div className="actions">
                  <button
                    disabled={
                      busy || !matches || operations.length > 0 || uncertain
                    }
                    onClick={resume}
                  >
                    恢复当前任务
                  </button>
                  <button
                    disabled={busy || operations.length > 0 || uncertain}
                    onClick={() => setCancelling(true)}
                  >
                    取消当前任务
                  </button>
                </div>
                {cancelling && (
                  <div className="task-cancel" role="alert">
                    <p>确定取消当前任务？未保存的建议将失效。</p>
                    <button disabled={busy} onClick={cancel}>
                      确定取消
                    </button>
                    <button onClick={() => setCancelling(false)}>
                      保留任务
                    </button>
                  </div>
                )}
                <details>
                  <summary>更正目标或追问</summary>
                  <form onSubmit={correct} className="memory-form">
                    <label>
                      更正目标票号
                      <input
                        value={correctTarget}
                        onChange={(e) => setCorrectTarget(e.target.value)}
                        maxLength={64}
                      />
                    </label>
                    <label>
                      更正追问
                      <input
                        value={question}
                        onChange={(e) => setQuestion(e.target.value)}
                        maxLength={2000}
                      />
                    </label>
                    <button
                      disabled={
                        busy ||
                        operations.length > 0 ||
                        uncertain ||
                        (!correctTarget && !question)
                      }
                    >
                      提交更正
                    </button>
                  </form>
                </details>
              </>
            ) : (
              <p className="empty">暂无未完成任务。发送问题后创建。</p>
            )}
          </article>
          {operations.map((op) => (
            <div className="notice warn" key={op.operation_id}>
              <strong>操作结果待核实</strong>
              <p>{op.operation_id}</p>
              <button disabled={busy} onClick={() => reconcile(op)}>
                核实操作结果
              </button>
            </div>
          ))}
          <details open>
            <summary>偏好</summary>
            <p className="muted">只影响表达和默认值，保存仍需人工确认。</p>
            <div className="memory-form">
              {source && <p>已选择一条本人消息作为来源</p>}
              <label>
                偏好内容
                <input
                  value={memoryContent}
                  maxLength={1000}
                  onChange={(e) => setMemoryContent(e.target.value)}
                />
              </label>
              <label>
                <input
                  type="checkbox"
                  checked={wide}
                  onChange={(e) => setWide(e.target.checked)}
                />
                用于本人所有已授权企业
              </label>
              <button
                disabled={busy || !source || !memoryContent.trim()}
                onClick={() => saveMemory()}
              >
                新增偏好
              </button>
            </div>
            {!memories.length && (
              <p className="empty">暂无偏好。可在用户消息上点击“记为偏好”。</p>
            )}
            {memories.map((m) => (
              <article className="memory-card" key={m.memory_id}>
                <p>{m.content}</p>
                <small>
                  {m.enterprise_id ? "本企业" : "所有已授权企业"} · 版本{" "}
                  {m.revision}
                </small>
                <div className="actions">
                  <button
                    disabled={busy || !memoryContent.trim()}
                    onClick={() => saveMemory(m)}
                  >
                    用输入内容更正
                  </button>
                  <button disabled={busy} onClick={() => saveMemory(m, true)}>
                    删除偏好
                  </button>
                  <button
                    onClick={() =>
                      run(async () => {
                        setAudit(
                          await api(
                            base + "/chat/memories/" + m.memory_id + "/audit",
                          ),
                        );
                      })
                    }
                  >
                    查看版本记录
                  </button>
                </div>
              </article>
            ))}
            {audit.length > 0 && (
              <div className="audit" aria-label="偏好版本记录">
                <h3>偏好版本记录</h3>
                {audit.map((row, index) => {
                  const item = row as {
                    action: string;
                    revision: number;
                    payload: string;
                  };
                  let content = "内容未返回";
                  try {
                    content = JSON.parse(item.payload).content || "已删除";
                  } catch {
                    /* Retain explicit unavailable state. */
                  }
                  return (
                    <p key={index}>
                      版本 {item.revision} ·{" "}
                      {{ created: "新增", corrected: "更正", deleted: "删除" }[
                        item.action
                      ] ?? item.action}
                      <br />
                      {content}
                    </p>
                  );
                })}
                <button onClick={() => setAudit([])}>收起版本记录</button>
              </div>
            )}
          </details>
        </aside>
        <section className="chat-panel" aria-label="对话">
          <div className="panel-title">
            <div>
              <span className="eyebrow">对话核验</span>
              <h2>从问题到可核对的结果</h2>
            </div>
            {final && (
              <span
                className={"tag " + (final === "succeeded" ? "good" : "warn")}
                data-testid="turn-status"
              >
                {statuses[final]}
              </span>
            )}
          </div>
          <div
            className="conversation"
            aria-live="polite"
            aria-label="对话消息"
          >
            {!messages.length && (
              <div className="empty">
                <h3>开始核对这家企业的资料</h3>
                <p>说明你要查询的范围，或者提供发票编号与待补充内容。</p>
              </div>
            )}
            {messages.map((m) => (
              <article className={"bubble " + m.role} key={m.message_id}>
                <small>
                  {m.role === "user"
                    ? "你"
                    : m.role === "system"
                      ? "系统回执"
                      : "核验助手"}
                </small>
                <p>{m.content}</p>
                {m.role === "user" && !m.message_id.startsWith("local-") && (
                  <button
                    disabled={busy}
                    onClick={() => {
                      setSource(m.message_id);
                      setMemoryContent(m.content.slice(0, 1000));
                      setTab("task");
                    }}
                  >
                    记为偏好
                  </button>
                )}
                {m.evidence?.map((e) => (
                  <button
                    className="evidence-link"
                    key={e.data.evidence_id}
                    onClick={() => focusEvidence(e.data.evidence_id)}
                  >
                    依据：{toolName(e.data.tool)} ·{" "}
                    {String(e.data.evidence_id).slice(0, 8)}
                  </button>
                ))}
              </article>
            ))}
            <div ref={bottom} />
          </div>
          <div className="examples">
            {[
              "查询本月发票合计、银行收入和支出",
              "F001 用途是否符合规则",
              "把 F001 备注补充为：待填写",
            ].map((x) => (
              <button key={x} disabled={busy} onClick={() => setMessage(x)}>
                {x}
              </button>
            ))}
          </div>
          <form className="composer" onSubmit={send}>
            <label htmlFor="chat-message">查询或补全要求</label>
            <textarea
              id="chat-message"
              value={message}
              maxLength={4000}
              rows={3}
              disabled={busy}
              onChange={(e) => setMessage(e.target.value)}
              onKeyDown={(e) => {
                if (
                  e.key === "Enter" &&
                  !e.shiftKey &&
                  !e.nativeEvent.isComposing
                ) {
                  e.preventDefault();
                  e.currentTarget.form?.requestSubmit();
                }
              }}
            />
            <div className="composer-footer">
              <small>{message.length} / 4000 · Shift+Enter 换行</small>
              <button
                className="primary"
                disabled={
                  busy ||
                  !session ||
                  !message.trim() ||
                  (!!proposal && matches && !resumed)
                }
              >
                发送
              </button>
            </div>
          </form>
          {status && (
            <p
              role="status"
              className={"notice " + (final === "incomplete" ? "warn" : "")}
            >
              {status}
              {final === "incomplete" && " 缩小问题后重试。"}
            </p>
          )}
        </section>
        <aside className="process-panel" aria-label="过程与建议">
          <div className="panel-title">
            <div>
              <span className="eyebrow">可复核的执行</span>
              <h2>过程与依据</h2>
            </div>
            <button
              disabled={busy || !currentTask.current}
              onClick={() => run(() => loadTraces())}
            >
              查看完整轨迹
            </button>
          </div>
          {proposal && (
            <article className="proposal-card" aria-label="待确认的发票建议">
              <h3>发票 {proposal.invoice_id} · 待确认</h3>
              {collapsed ? (
                <button onClick={() => setCollapsed(false)}>展开建议</button>
              ) : (
                <>
                  <p>读取版本 {proposal.proposal.expected_revision}</p>
                  <table>
                    <thead>
                      <tr>
                        <th>字段</th>
                        <th>改前</th>
                        <th>改后</th>
                      </tr>
                    </thead>
                    <tbody>
                      {Object.entries(proposal.proposal.changes).map(
                        ([f, v]) => (
                          <tr key={f}>
                            <td>{f === "purpose" ? "用途" : "备注"}</td>
                            <td>
                              {proposal.before[f as "note" | "purpose"] ??
                                "未填写"}
                            </td>
                            <td>{v}</td>
                          </tr>
                        ),
                      )}
                    </tbody>
                  </table>
                  <p>
                    {proposal.proposal.rule_ref.source} · 版本{" "}
                    {proposal.proposal.rule_ref.version} · 条款{" "}
                    {proposal.proposal.rule_ref.clause}
                  </p>
                  <p className="muted">
                    依据：当前发票读取版本、用户补充内容与上述适用规则。
                  </p>
                  {reason && <p className="notice warn">{reason}</p>}
                  <div className="actions">
                    <button
                      className="primary"
                      disabled={!!reason || busy}
                      onClick={confirm}
                    >
                      确认保存
                    </button>
                    <button disabled={busy} onClick={() => setCollapsed(true)}>
                      收起建议
                    </button>
                  </div>
                </>
              )}
            </article>
          )}
          {savePhase && (
            <p className="notice" role="status">
              {savePhase}
            </p>
          )}
          {uncertain && (
            <button disabled={busy} onClick={() => reconcile()}>
              核实操作结果
            </button>
          )}
          <ProcessPanel rounds={rounds} selected={selected} />
        </aside>
      </section>
    </>
  );
}
