"use client";
import { FormEvent, useState, useEffect } from "react";
import InvoiceWorkflow, { api, type Enterprise } from "./invoice-workflow";
import RuleWorkflow from "./rule-workflow";
import ImportWorkflow from "./import-workflow";
import MemberWorkflow from "./member-workflow";
import BankWorkflow from "./bank-workflow";
import ChatWorkflow from "./chat-workflow";

export default function Home() {
  const [enterprises, setEnterprises] = useState<Enterprise[]>([]),
    [username, setUsername] = useState("");
  const [loading, setLoading] = useState(true),
    [busy, setBusy] = useState(false),
    [status, setStatus] = useState("");
  const [view, setView] = useState("chat"),
    [scope, setScope] = useState(""),
    [model, setModel] = useState("模型待验证");
  const [target, setTarget] = useState<{
    enterprise_id: string;
    invoice_id: string;
  }>();
  async function load(identity: { username: string }) {
    const [items, access] = await Promise.all([
      api("/api/enterprises"),
      api("/api/enterprise-access"),
    ]);
    const grants = new Map<string, Enterprise["role"]>(
      access.map((r: Enterprise) => [r.enterprise_id, r.role]),
    );
    setEnterprises(
      items.flatMap((r: Enterprise) =>
        grants.has(r.enterprise_id)
          ? [{ ...r, role: grants.get(r.enterprise_id) }]
          : [],
      ),
    );
    setUsername(identity.username);
  }
  useEffect(() => {
    let live = true;
    fetch("/api/session", { cache: "no-store" })
      .then(async (r) => {
        if (r.ok) {
          const identity = await r.json();
          if (live) await load(identity);
        } else if (r.status !== 401)
          throw new Error("会话检查失败，请刷新重试。");
      })
      .catch((e) => {
        if (live) setStatus(e.message);
      })
      .finally(() => {
        if (live) setLoading(false);
      });
    const expired = () => {
      setUsername("");
      setEnterprises([]);
      setStatus("会话已过期，请重新登录。");
    };
    window.addEventListener("session-expired", expired);
    return () => {
      live = false;
      window.removeEventListener("session-expired", expired);
    };
  }, []);
  async function login(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    setBusy(true);
    try {
      await load(
        await api("/api/session", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            username: f.get("username"),
            password: f.get("password"),
          }),
        }),
      );
      setStatus("");
    } catch (e) {
      setStatus(e instanceof Error ? e.message : "登录失败。");
    } finally {
      setBusy(false);
    }
  }
  async function logout() {
    setBusy(true);
    try {
      await api("/api/session", { method: "DELETE" });
      setUsername("");
      setEnterprises([]);
      setTarget(undefined);
      setStatus("已退出登录。");
    } catch (e) {
      setStatus(String(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <main className="app-shell">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark">税</span>
          <div>
            <strong>财税资料核验工作台</strong>
            <small>对话 · 核验 · 人工确认</small>
          </div>
        </div>
        {username && (
          <>
            <nav aria-label="工作台导航">
              <button
                aria-pressed={view === "chat"}
                onClick={() => setView("chat")}
              >
                对话工作台
              </button>
              <button
                aria-pressed={view === "data"}
                onClick={() => setView("data")}
              >
                业务数据
              </button>
            </nav>
            <div className="account">
              <span>{scope}</span>
              <span className="tag" data-testid="model-status">
                {model}
              </span>
              <span>{username}</span>
              <button disabled={busy} onClick={logout}>
                退出登录
              </button>
            </div>
          </>
        )}
      </header>
      {loading ? (
        <p role="status" className="login-card">
          正在恢复登录状态…
        </p>
      ) : !username ? (
        <section className="login-card">
          <span className="eyebrow">财税工作空间</span>
          <h1>登录工作台</h1>
          <p>查询资料、核对依据，由你确认每一次保存。</p>
          <form onSubmit={login}>
            <label>
              账户
              <input name="username" autoComplete="username" required />
            </label>
            <label>
              密码
              <input
                name="password"
                type="password"
                autoComplete="current-password"
                required
              />
            </label>
            <button className="primary" disabled={busy}>
              登录
            </button>
          </form>
          <p role="status">{status}</p>
        </section>
      ) : (
        <>
          {status && (
            <p role="status" className="notice">
              {status}
            </p>
          )}
          {!enterprises.length ? (
            <p className="notice">当前账户没有企业授权，请联系管理员。</p>
          ) : (
            <>
              <div hidden={view !== "chat"}>
                <ChatWorkflow
                  key={username}
                  enterprises={enterprises}
                  onScope={setScope}
                  onModel={setModel}
                />
              </div>
              <section
                className="data-page"
                hidden={view !== "data"}
                aria-label="业务数据"
              >
                <div className="page-intro">
                  <span className="eyebrow">业务数据</span>
                  <h1>资料与权限</h1>
                  <p>查看和维护已授权企业的业务资料。</p>
                </div>
                <div className="data-grid">
                  <ImportWorkflow
                    enterprises={enterprises}
                    inspect={setTarget}
                  />
                  <InvoiceWorkflow
                    key={
                      target
                        ? target.enterprise_id + target.invoice_id
                        : "default"
                    }
                    enterprises={enterprises}
                    target={target}
                  />
                  <RuleWorkflow enterprises={enterprises} />
                  <BankWorkflow enterprises={enterprises} />
                  {enterprises.some((e) => e.role === "admin") && (
                    <MemberWorkflow
                      enterprises={enterprises.filter(
                        (e) => e.role === "admin",
                      )}
                    />
                  )}
                </div>
              </section>
            </>
          )}
        </>
      )}
    </main>
  );
}
