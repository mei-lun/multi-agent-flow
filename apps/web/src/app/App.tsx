import { FormEvent, ReactNode, useEffect, useState } from "react";
import { Link, NavLink, useNavigate, useParams } from "react-router-dom";
import { AppRouter } from "./router";
import { ApiError, authToken, get, post, setAuthToken } from "../api/client";
import type { InboxItem, LoginResponse, ModelConnection, Page, Project, Run, Task, User } from "../api/contracts";
import { openEventStream } from "../api/events";

export function App() { return authToken() ? <Shell><AppRouter /></Shell> : <LoginPage />; }

export function LoginPage() {
  const navigate = useNavigate(); const [username, setUsername] = useState(""); const [password, setPassword] = useState(""); const [error, setError] = useState(""); const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent) { event.preventDefault(); setBusy(true); setError(""); try { const result = await post<LoginResponse>("/api/v1/auth/login", { username, password }); if (result.access_token) setAuthToken(result.access_token); navigate("/dashboard"); window.location.reload(); } catch (e) { setError(e instanceof ApiError ? `${e.message}${e.traceId ? `（Trace ID: ${e.traceId}）` : ""}` : "登录失败"); } finally { setBusy(false); } }
  return <main className="login"><form onSubmit={submit} className="login-card"><h1>Multi-Agent Flow</h1><p>统一管理项目、工作流与运行任务</p><label>用户名<input value={username} onChange={e => setUsername(e.target.value)} required autoComplete="username" /></label><label>密码<input type="password" value={password} onChange={e => setPassword(e.target.value)} required autoComplete="current-password" /></label>{error && <div className="error">{error}</div>}<button disabled={busy}>{busy ? "登录中…" : "登录"}</button></form></main>;
}

function Shell({ children }: { children: ReactNode }) {
  const navigate = useNavigate(); const [user, setUser] = useState<User | null>(null);
  useEffect(() => { void get<User>("/api/v1/me").then(setUser).catch(() => { setAuthToken(null); navigate("/login"); }); }, [navigate]);
  async function logout() { try { await post<void>("/api/v1/auth/logout"); } finally { setAuthToken(null); navigate("/login"); window.location.reload(); } }
  const links = [["/dashboard", "Dashboard"], ["/projects", "项目"], ["/models", "模型"], ["/workflows", "工作流"], ["/inbox", "Inbox"], ["/settings", "设置"]];
  return <div className="shell"><aside><Link className="brand" to="/dashboard">MAF</Link><nav>{links.map(([to, label]) => <NavLink key={to} to={to}>{label}</NavLink>)}</nav><button className="logout" onClick={logout}>退出登录</button></aside><section className="content"><header><span>{user?.display_name ?? user?.username ?? ""}</span></header><div className="page">{children}</div></section></div>;
}

function useResource<T>(path: string) { const [data, setData] = useState<T | null>(null); const [error, setError] = useState(""); useEffect(() => { let alive = true; void get<T>(path).then(v => alive && setData(v)).catch(e => alive && setError(e instanceof Error ? e.message : "加载失败")); return () => { alive = false; }; }, [path]); return { data, error }; }
function State({ children }: { children: ReactNode }) { return <div className="state">{children}</div>; }
function PageTitle({ title, action }: { title: string; action?: ReactNode }) { return <div className="page-title"><h2>{title}</h2>{action}</div>; }

export function DashboardPage() { const projects = useResource<Page<Project> | Project[]>("/api/v1/projects"); const inbox = useResource<Page<InboxItem> | InboxItem[]>("/api/v1/inbox?status=PENDING"); const list = (v: Page<Project> | Project[] | null): Project[] => Array.isArray(v) ? v : v?.items ?? []; const pending = inbox.data && (Array.isArray(inbox.data) ? inbox.data : inbox.data.items)?.length || 0; return <><PageTitle title="Dashboard" /><div className="stats"><div><b>{list(projects.data).length}</b><span>项目</span></div><div><b>{pending}</b><span>待处理</span></div></div><section className="panel"><h3>项目概览</h3>{projects.error ? <State>{projects.error}</State> : projects.data === null ? <State>加载中…</State> : <table><thead><tr><th>名称</th><th>状态</th><th>更新时间</th></tr></thead><tbody>{list(projects.data).map(p => <tr key={p.id}><td><Link to={`/projects?id=${p.id}`}>{p.name}</Link></td><td>{p.status ?? "-"}</td><td>{p.updated_at ? new Date(p.updated_at).toLocaleString() : "-"}</td></tr>)}</tbody></table>}</section></>; }

export function ProjectsPage() { const { data, error } = useResource<Page<Project> | Project[]>("/api/v1/projects"); const projects = Array.isArray(data) ? data : data?.items ?? []; return <><PageTitle title="项目" action={<Link className="button" to="/dashboard">返回 Dashboard</Link>} />{error ? <State>{error}</State> : data === null ? <State>加载中…</State> : <div className="grid">{projects.map(p => <article className="card" key={p.id}><h3>{p.name}</h3><p>{p.description || "暂无描述"}</p><small>{p.status ?? "UNKNOWN"} · {p.repository_count ?? 0} 个仓库</small></article>)}{projects.length === 0 && <State>暂无项目</State>}</div>}</>; }

export function ModelPage() { const { data, error } = useResource<Page<ModelConnection> | ModelConnection[]>("/api/v1/model-connections"); const models = Array.isArray(data) ? data : data?.items ?? []; return <><PageTitle title="模型连接" /><section className="panel">{error ? <State>{error}</State> : data === null ? <State>加载中…</State> : <table><thead><tr><th>名称</th><th>供应商</th><th>状态</th></tr></thead><tbody>{models.map(m => <tr key={m.id}><td>{m.name}</td><td>{m.provider ?? "-"}</td><td>{m.status ?? "未验证"}</td></tr>)}</tbody></table>}</section></>; }

export function WorkflowPage() { return <><PageTitle title="Workflow 设计器" /><section className="panel workflow"><p>选择项目后编辑工作流图。保存和发布操作将通过服务端校验 expected_version。</p><div className="canvas"><span>开始</span><i>→</i><span>执行节点</span><i>→</i><span>结束</span></div><button onClick={() => alert("请先选择项目")}>保存草稿</button></section></>; }

export function RunPage() { const { runId = "" } = useParams(); const run = useResource<Run>(`/api/v1/runs/${runId}`); const tasks = useResource<Page<Task> | Task[]>(`/api/v1/runs/${runId}/tasks`); const [events, setEvents] = useState<string[]>([]); useEffect(() => openEventStream(`/api/v1/runs/${runId}/events`, { onEvent: e => setEvents(v => [...v.slice(-49), JSON.stringify(e.data)]) }), [runId]); const list = Array.isArray(tasks.data) ? tasks.data : tasks.data?.items ?? []; return <><PageTitle title={`Run ${runId}`} /><section className="panel">{run.data ? <><p>状态：<strong>{run.data.status}</strong></p><p>Control commit：{run.data.control_commit ?? "-"}</p></> : <State>{run.error || "加载中…"}</State>}</section><section className="panel"><h3>任务</h3>{list.map(t => <div className="row" key={t.id}><span>{t.name ?? t.id}</span><span>{t.status}</span><span>{t.progress ?? 0}%</span></div>)}</section><section className="panel"><h3>事件</h3><pre>{events.join("\n") || "等待事件…"}</pre></section></>; }

export function InboxPage() { const { data, error } = useResource<Page<InboxItem> | InboxItem[]>("/api/v1/inbox"); const items = Array.isArray(data) ? data : data?.items ?? []; return <><PageTitle title="Inbox" /><section className="panel">{error ? <State>{error}</State> : data === null ? <State>加载中…</State> : items.length ? items.map(item => <article className="inbox-item" key={item.id}><div><b>{item.title ?? item.type ?? "待处理事项"}</b><small>{item.project_id ?? ""}</small></div><span>{item.status}</span></article>) : <State>暂无待办</State>}</section></>; }

export function SettingsPage() { return <><PageTitle title="设置" /><section className="panel"><h3>节点与审计</h3><p>Secret 仅展示配置状态，不会回显密钥。所有写操作由服务端鉴权并记录审计。</p><Link className="button" to="/dashboard">返回</Link></section></>; }
