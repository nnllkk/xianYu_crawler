"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import {
  Activity,
  CircleAlert,
  CircleStop,
  Edit3,
  FileKey2,
  Mail,
  Play,
  Plus,
  Power,
  Radar,
  RefreshCw,
  RotateCw,
  Save,
  ShieldCheck,
  Trash2,
  UserRound,
  X,
} from "lucide-react";

const API = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:8000";
type Rule = {
  id: string;
  product: string;
  extra_conditions: string | null;
  budget: string | null;
  interval_minutes: number;
  is_enabled: boolean;
  next_run_at: string | null;
  emails: string[];
  parsed_requirement: { conditions?: string[] } | null;
};
type Task = {
  id: string;
  status: string;
  stage: string;
  scraped_count: number;
  candidate_count: number;
  sent_count: number;
  error_message: string | null;
  started_at: string | null;
};
type TaskPage = {
  items: Task[];
  total: number;
  page: number;
  page_size: number;
};
type Log = { id: string; price: number; email: string; created_at: string };
type Account = {
  id: string;
  name: string;
  status: string;
  failure_count: number;
  last_error: string | null;
  cooldown_until: string | null;
  last_used_at: string | null;
  is_enabled: boolean;
  state_file: string;
  state_exists: boolean;
};
type Draft = {
  product: string;
  extra_conditions: string;
  budget: string;
  emails: string;
  interval_minutes: number;
  enabled: boolean;
};
const empty: Draft = {
  product: "",
  extra_conditions: "",
  budget: "",
  emails: "",
  interval_minutes: 15,
  enabled: true,
};
const stages: Record<string, string> = {
  pending: "等待中",
  parsing: "解析商品",
  scraping: "检索商品",
  filtering: "规则筛选",
  ranking: "比较候选",
  sending: "发送邮件",
  done: "已完成",
  failed: "失败",
  stopped: "已停止",
};
const parts = (s: string) =>
  Array.from(
    new Set(
      s
        .split(/[,，\n]/)
        .map((x) => x.trim())
        .filter(Boolean),
    ),
  );
// 后端以 MySQL DATETIME 保存北京时间；无偏移字符串须显式按 +08:00 解释。
const date = (s: string | null) =>
  s
    ? new Intl.DateTimeFormat("zh-CN", {
        month: "numeric",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
        timeZone: "Asia/Shanghai",
      }).format(new Date(/[zZ]$|[+-]\d\d:\d\d$/.test(s) ? s : `${s}+08:00`))
    : "等待调度器计算";
async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(`${API}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!r.ok) {
    const b = await r.json().catch(() => null);
    throw Error(b?.detail ?? `请求失败 (${r.status})`);
  }
  return r.json();
}

export default function Home() {
  const [rules, setRules] = useState<Rule[]>([]),
    [picked, setPicked] = useState<string | null>(() =>
      typeof window === "undefined"
        ? null
        : localStorage.getItem("xianyu-picked-rule"),
    ),
    [tasks, setTasks] = useState<Task[]>([]),
    [logs, setLogs] = useState<Log[]>([]),
    [accounts, setAccounts] = useState<Account[]>([]),
    [activeTab, setActiveTab] = useState<"rules" | "accounts">("rules"),
    [draft, setDraft] = useState<Draft>(empty),
    [editing, setEditing] = useState<string | null>(null),
    [busy, setBusy] = useState(""),
    [notice, setNotice] = useState(""),
    [online, setOnline] = useState(false),
    [failure, setFailure] = useState<Task | null>(null),
    [taskPage, setTaskPage] = useState(1),
    [taskTotal, setTaskTotal] = useState(0);
  const current = rules.find((x) => x.id === picked) ?? null;
  const load = useCallback(async () => {
    try {
      const items = await call<Rule[]>("/api/rules");
      setRules(items);
      setPicked((old) => {
        const next = items.some((x) => x.id === old)
          ? old
          : (items[0]?.id ?? null);
        if (next) localStorage.setItem("xianyu-picked-rule", next);
        else localStorage.removeItem("xianyu-picked-rule");
        return next;
      });
      setOnline(true);
    } catch (e) {
      setOnline(false);
      setNotice(e instanceof Error ? e.message : "无法连接后端");
    }
  }, []);
  const loadAccounts = useCallback(async () => {
    try {
      setAccounts(await call<Account[]>("/api/xianyu-accounts"));
    } catch (e) {
      setNotice(e instanceof Error ? e.message : "读取账号失败");
    }
  }, []);
  const loadDetails = useCallback(
    async (id: string, page = taskPage) => {
      try {
        const [a, b] = await Promise.all([
          call<TaskPage>(`/api/rules/${id}/tasks?page=${page}&page_size=8`),
          call<Log[]>(`/api/rules/${id}/history`),
        ]);
        setTasks(a.items);
        setTaskTotal(a.total);
        setLogs(b);
      } catch (e) {
        setNotice(e instanceof Error ? e.message : "读取记录失败");
      }
    },
    [taskPage],
  );
  useEffect(() => {
    void load();
    void loadAccounts();
  }, [load, loadAccounts]);
  useEffect(() => {
    setTaskPage(1);
    if (picked) void loadDetails(picked, 1);
  }, [picked]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (picked && taskPage > 1) void loadDetails(picked, taskPage);
  }, [picked, taskPage, loadDetails]);
  useEffect(() => {
    const id = setInterval(() => {
      void load();
      void loadAccounts();
      if (picked) void loadDetails(picked, taskPage);
    }, 10000);
    return () => clearInterval(id);
  }, [load, loadAccounts, loadDetails, picked, taskPage]);
  const change = (key: keyof Draft, value: string | number | boolean) =>
    setDraft((v) => ({ ...v, [key]: value }));
  async function save(e: FormEvent) {
    e.preventDefault();
    const emails = parts(draft.emails);
    if (!draft.product.trim() || !emails.length) {
      setNotice("请填写商品名称和至少一个收件邮箱");
      return;
    }
    setBusy("save");
    try {
      const payload = {
        product: draft.product.trim(),
        extra_conditions: draft.extra_conditions || null,
        budget: draft.budget || null,
        emails,
        interval_minutes: Number(draft.interval_minutes),
        enabled: draft.enabled,
      };
      const rule = await call<Rule>(
        editing ? `/api/rules/${editing}` : "/api/rules",
        { method: editing ? "PUT" : "POST", body: JSON.stringify(payload) },
      );
      await load();
      setPicked(rule.id);
      localStorage.setItem("xianyu-picked-rule", rule.id);
      setDraft(empty);
      setEditing(null);
      setNotice("规则已保存");
    } catch (err) {
      setNotice(err instanceof Error ? err.message : "保存失败");
    } finally {
      setBusy("");
    }
  }
  async function action(rule: Rule, kind: "run" | "start" | "stop") {
    setBusy(kind + rule.id);
    // 立即执行后刷新规则列表时，必须保持用户刚点击的规则，否则记录会被另一条规则覆盖。
    setPicked(rule.id);
    localStorage.setItem("xianyu-picked-rule", rule.id);
    try {
      await call(
        kind === "run"
          ? `/api/rules/${rule.id}/run`
          : `/api/rules/${rule.id}/${kind}`,
        { method: "POST" },
      );
      await load();
      await loadDetails(rule.id);
      setNotice(
        kind === "run"
          ? "任务已开始，运行结果会显示在记录中"
          : kind === "start"
            ? "监控已启用"
            : "监控已停止",
      );
    } catch (err) {
      setNotice(err instanceof Error ? err.message : "操作失败");
    } finally {
      setBusy("");
    }
  }
  async function createAccount(e: FormEvent) {
    e.preventDefault();
    const name = `account-${Date.now()}`;
    setBusy("account-create");
    try {
      await call<Account>("/api/xianyu-accounts", {
        method: "POST",
        body: JSON.stringify({ name }),
      });
      await loadAccounts();
      setNotice("登录窗口已打开，请完成闲鱼登录");
    } catch (err) {
      setNotice(err instanceof Error ? err.message : "添加账号失败");
    } finally {
      setBusy("");
    }
  }
  async function toggleAccount(account: Account) {
    const actionName = account.is_enabled ? "disable" : "enable";
    setBusy(actionName + account.id);
    try {
      await call(`/api/xianyu-accounts/${account.id}/${actionName}`, {
        method: "POST",
      });
      await loadAccounts();
      setNotice(account.is_enabled ? "账号已停用" : "账号已启用");
    } catch (err) {
      setNotice(err instanceof Error ? err.message : "账号操作失败");
    } finally {
      setBusy("");
    }
  }
  async function deleteAccount(account: Account) {
    if (!window.confirm(`确定删除账号“${account.name}”及其登录状态 JSON 吗？`))
      return;
    setBusy("delete" + account.id);
    try {
      await call(`/api/xianyu-accounts/${account.id}`, { method: "DELETE" });
      await loadAccounts();
      setNotice("账号和登录状态 JSON 已删除");
    } catch (err) {
      setNotice(err instanceof Error ? err.message : "删除账号失败");
    } finally {
      setBusy("");
    }
  }
  async function deleteRule(rule: Rule) {
    if (!window.confirm(`确定删除监控规则“${rule.product}”吗？相关运行记录也会删除。`))
      return;
    setBusy(`delete-rule-${rule.id}`);
    try {
      await call(`/api/rules/${rule.id}`, { method: "DELETE" });
      if (editing === rule.id) {
        setEditing(null);
        setDraft(empty);
      }
      if (picked === rule.id) {
        setPicked(null);
        localStorage.removeItem("xianyu-picked-rule");
        setTasks([]);
        setLogs([]);
        setTaskTotal(0);
      }
      await load();
      setNotice("规则已删除");
    } catch (err) {
      setNotice(err instanceof Error ? err.message : "删除规则失败");
    } finally {
      setBusy("");
    }
  }
  function edit(r: Rule) {
    setEditing(r.id);
    setDraft({
      product: r.product,
      extra_conditions: r.extra_conditions ?? "",
      budget: r.budget ?? "",
      emails: r.emails.join("，"),
      interval_minutes: r.interval_minutes,
      enabled: r.is_enabled,
    });
    window.scrollTo({ top: 0, behavior: "smooth" });
  }
  return (
    <main className="shell">
      <aside className="rail">
        <div className="brand">
          <i>
            <Radar size={18} />
          </i>
          闲鱼雷达
        </div>
        <nav>
          <button
            className={`nav ${activeTab === "rules" ? "active" : ""}`}
            onClick={() => setActiveTab("rules")}
          >
            <Activity size={16} />
            监控规则
          </button>
          <button
            className={`nav ${activeTab === "accounts" ? "active" : ""}`}
            onClick={() => setActiveTab("accounts")}
          >
            <UserRound size={16} />
            闲鱼账号
          </button>
        </nav>
        <p>
          本机运行
          <br />
          采集、筛选与邮件推送
        </p>
      </aside>
      <section className="work">
        <header>
          <div>
            <h1>{activeTab === "rules" ? "商品监控工作台" : "闲鱼账号管理"}</h1>
            <p>
              {activeTab === "rules"
                ? "设置一次，系统会持续检索闲鱼列表并发送新商品。"
                : "管理已授权的登录状态 JSON，供采集任务安全复用。"}
            </p>
          </div>
          <span className={`online ${online ? "" : "off"}`}>
            {online ? "后端已连接" : "后端未连接"}
          </span>
        </header>
        {activeTab === "accounts" && (
          <section className="account-panel">
            <div className="account-intro">
              <div>
                <span className="eyebrow">
                  <ShieldCheck size={14} />
                  本地登录状态
                </span>
                <h2>多个账号，按需切换</h2>
                <p>
                  页面只展示状态和文件名，不读取或展示 Cookie
                  内容。触发平台验证的账号会自动冷却，任务再尝试其他已启用账号。
                </p>
              </div>
              <form className="account-create" onSubmit={createAccount}>
                <button
                  className="primary"
                  disabled={busy === "account-create"}
                >
                  <Plus size={15} />
                  添加账号
                </button>
              </form>
            </div>
            <div className="account-list">
              <div className="section-title">
                登录状态 JSON{" "}
                <button className="text" onClick={() => void loadAccounts()}>
                  <RefreshCw size={14} />
                  刷新
                </button>
              </div>
              {accounts.length === 0 ? (
                <div className="empty">
                  还没有账号。添加账号后会自动打开登录窗口。
                </div>
              ) : (
                accounts.map((account) => (
                  <article className="account-row" key={account.id}>
                    <div className="account-name">
                      <span
                        className={
                          account.is_enabled
                            ? "account-dot ready"
                            : "account-dot"
                        }
                      />
                      <strong>{account.name}</strong>
                      <small>{account.state_file}</small>
                    </div>
                    <div>
                      <span className={`account-status ${account.status}`}>
                        {account.status === "active"
                          ? "可用"
                          : account.status === "cooldown"
                            ? "冷却中"
                            : account.status === "login_pending"
                              ? "等待登录"
                              : "待登录"}
                      </span>
                      <small>
                        {account.status === "login_pending"
                          ? "浏览器已打开，请完成闲鱼登录"
                          : account.state_exists
                            ? "JSON 文件已存在"
                            : "尚未生成 JSON 文件"}
                      </small>
                    </div>
                    <div>
                      <strong>
                        {account.cooldown_until
                          ? `冷却至 ${date(account.cooldown_until)}`
                          : account.last_used_at
                            ? `上次使用 ${date(account.last_used_at)}`
                            : "尚未使用"}
                      </strong>
                      <small>{account.last_error ?? "没有失败记录"}</small>
                    </div>
                    <div className="account-actions">
                      <button
                        title={account.is_enabled ? "停用账号" : "启用账号"}
                        className={account.is_enabled ? "danger" : ""}
                        onClick={() => void toggleAccount(account)}
                        disabled={
                          busy ===
                          `${account.is_enabled ? "disable" : "enable"}${account.id}`
                        }
                      >
                        <Power size={16} />
                      </button>
                      <button
                        title="删除账号和登录状态 JSON"
                        className="danger"
                        onClick={() => void deleteAccount(account)}
                        disabled={busy === `delete${account.id}`}
                      >
                        <Trash2 size={16} />
                      </button>
                    </div>
                  </article>
                ))
              )}
            </div>
            <div className="account-note">
              <FileKey2 size={17} />
              <p>
                添加账号会打开可见浏览器。请手动完成闲鱼登录和验证，状态文件会自动写入{" "}
                <code>backend/data/xianyu_states/</code>，该目录不会提交到 Git。
              </p>
            </div>
          </section>
        )}
        {activeTab === "rules" && (
          <>
            <section className="stats">
              <div>
                <small>启用中的规则</small>
                <b>{rules.filter((x) => x.is_enabled).length}</b>
                <em>共 {rules.length} 条规则</em>
              </div>
              <div>
                <small>当前规则推送</small>
                <b>{logs.length}</b>
                <em>已发送商品数量</em>
              </div>
              <div>
                <small>最近任务推送</small>
                <b>{tasks[0]?.sent_count ?? "—"}</b>
                <em>最新一次执行结果</em>
              </div>
            </section>
            <section className="composer">
              <div className="section-title">
                {editing ? "编辑监控规则" : "新建监控规则"}
                {editing && (
                  <button
                    className="text"
                    type="button"
                    onClick={() => {
                      setEditing(null);
                      setDraft(empty);
                    }}
                  >
                    取消编辑
                  </button>
                )}
              </div>
              <form onSubmit={save}>
                <label className="product">
                  想找什么商品
                  <input
                    value={draft.product}
                    onChange={(e) => change("product", e.target.value)}
                    placeholder="例如：MacBook M1 Pro"
                  />
                </label>
                <label>
                  预算范围
                  <input
                    value={draft.budget}
                    onChange={(e) => change("budget", e.target.value)}
                    placeholder="6000-8000（可空）"
                  />
                </label>
                <label>
                  收件邮箱
                  <input
                    value={draft.emails}
                    onChange={(e) => change("emails", e.target.value)}
                    placeholder="多个邮箱用逗号分隔"
                  />
                </label>
                <label className="wide">
                  补充条件
                  <textarea
                    value={draft.extra_conditions}
                    onChange={(e) => change("extra_conditions", e.target.value)}
                    placeholder="例如：16寸 32G+512G，成色好，不要维修机或企业管理机"
                  />
                </label>
                <label>
                  执行频率
                  <select
                    value={draft.interval_minutes}
                    onChange={(e) =>
                      change("interval_minutes", Number(e.target.value))
                    }
                  >
                    {[10, 15, 20, 30].map((x) => (
                      <option key={x}>{x}</option>
                    ))}
                  </select>
                </label>
                <div className="form-end">
                  <label className="toggle">
                    <input
                      type="checkbox"
                      checked={draft.enabled}
                      onChange={(e) => change("enabled", e.target.checked)}
                    />
                    <span />
                    {editing ? "保存后保持启用状态" : "创建后自动启用"}
                  </label>
                  <button className="primary" disabled={busy === "save"}>
                    {editing ? <Save size={15} /> : <Plus size={15} />}
                    {editing ? "保存修改" : "创建规则"}
                  </button>
                </div>
              </form>
            </section>
            <div className="list-head">
              <div>
                <h2>监控规则</h2>
                <p>后台会按 10–30 分钟随机间隔执行，页面时间均为北京时间。</p>
              </div>
              <button className="text" onClick={() => void load()}>
                <RefreshCw size={14} />
                刷新
              </button>
            </div>
            <section className="rules">
              {rules.length === 0 ? (
                <div className="empty">
                  还没有规则。填写上方表单后即可开始监控。
                </div>
              ) : (
                rules.map((r) => (
                  <article
                    className={picked === r.id ? "rule chosen" : "rule"}
                    key={r.id}
                    onClick={() => {
                      setPicked(r.id);
                      localStorage.setItem("xianyu-picked-rule", r.id);
                    }}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        setPicked(r.id);
                        localStorage.setItem("xianyu-picked-rule", r.id);
                      }
                    }}
                    tabIndex={0}
                    aria-current={picked === r.id ? "true" : undefined}
                  >
                    <div className="name">
                      <span className={r.is_enabled ? "tag on" : "tag"}>
                        {r.is_enabled ? "监控中" : "已停止"}
                      </span>
                      <button
                        onClick={() => {
                          setPicked(r.id);
                          localStorage.setItem("xianyu-picked-rule", r.id);
                        }}
                      >
                        {r.product}
                      </button>
                      <small>
                        {r.extra_conditions || "未设置额外条件"}
                        {r.budget ? ` · ¥${r.budget}` : " · 价格优先"}
                      </small>
                    </div>
                    <div>
                      <strong>{r.emails.join("、")}</strong>
                      <small>每 {r.interval_minutes} 分钟检查</small>
                    </div>
                    <div>
                      <strong>
                        {r.is_enabled ? "下次执行" : "监控已暂停"}
                      </strong>
                      <small>
                        {r.is_enabled ? date(r.next_run_at) : "启用后开始排程"}
                      </small>
                    </div>
                    <div className="actions">
                      <button title="编辑规则" onClick={() => edit(r)}>
                        <Edit3 size={16} />
                      </button>
                      <button
                        title="删除规则"
                        className="danger"
                        disabled={busy === `delete-rule-${r.id}`}
                        onClick={(event) => {
                          event.stopPropagation();
                          void deleteRule(r);
                        }}
                      >
                        <Trash2 size={16} />
                      </button>
                      <button
                        title="立即执行"
                        disabled={busy === `run${r.id}`}
                        onClick={() => void action(r, "run")}
                      >
                        <Play size={16} />
                      </button>
                      <button
                        title={r.is_enabled ? "停止监控" : "启用监控"}
                        className={r.is_enabled ? "danger" : ""}
                        onClick={() =>
                          void action(r, r.is_enabled ? "stop" : "start")
                        }
                      >
                        {r.is_enabled ? (
                          <CircleStop size={16} />
                        ) : (
                          <RotateCw size={16} />
                        )}
                      </button>
                    </div>
                  </article>
                ))
              )}
            </section>
            {current && (
              <section className="records">
                <div>
                  <div className="section-title">
                    {current.product} 的运行记录
                  </div>
                  <table>
                    <thead>
                      <tr>
                        <th>开始时间</th>
                        <th>状态</th>
                        <th>阶段</th>
                        <th>采集 / 候选 / 推送</th>
                        <th>原因</th>
                      </tr>
                    </thead>
                    <tbody>
                      {tasks.length ? (
                        tasks.map((t) => (
                          <tr key={t.id}>
                            <td>{date(t.started_at)}</td>
                            <td className={t.status}>
                              {t.status === "success"
                                ? "成功"
                                : t.status === "failed"
                                  ? "失败"
                                  : "执行中"}
                            </td>
                            <td>{stages[t.stage] ?? t.stage}</td>
                            <td>
                              {t.scraped_count} / {t.candidate_count} /{" "}
                              {t.sent_count}
                            </td>
                            <td>
                              {t.status === "failed" ? (
                                <button
                                  className="failure-button"
                                  onClick={() => setFailure(t)}
                                >
                                  <CircleAlert size={14} />
                                  查看原因
                                </button>
                              ) : (
                                "—"
                              )}
                            </td>
                          </tr>
                        ))
                      ) : (
                        <tr>
                          <td colSpan={5}>等待首次执行</td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                  <div className="pagination">
                    <span>
                      共 {taskTotal} 条 · 第 {taskPage} /{" "}
                      {Math.max(1, Math.ceil(taskTotal / 8))} 页
                    </span>
                    <div>
                      <button
                        disabled={taskPage <= 1}
                        onClick={() =>
                          setTaskPage((page) => Math.max(1, page - 1))
                        }
                      >
                        上一页
                      </button>
                      <button
                        disabled={
                          taskPage >= Math.max(1, Math.ceil(taskTotal / 8))
                        }
                        onClick={() => setTaskPage((page) => page + 1)}
                      >
                        下一页
                      </button>
                    </div>
                  </div>
                </div>
                <aside>
                  <div className="section-title">需求解析</div>
                  <p className="hint">用于比较商品的条件</p>
                  <div className="chips">
                    {current.parsed_requirement?.conditions?.map((x) => (
                      <span key={x}>{x}</span>
                    )) ?? <span>将在创建规则时生成</span>}
                  </div>
                  <div className="section-title">已发送商品</div>
                  {logs.length ? (
                    logs.slice(0, 5).map((x) => (
                      <p className="mail" key={x.id}>
                        ¥{x.price}
                        <span>{x.email}</span>
                        <small>{date(x.created_at)}</small>
                      </p>
                    ))
                  ) : (
                    <p className="empty">尚未发送商品</p>
                  )}
                </aside>
              </section>
            )}
          </>
        )}
        {failure && (
          <div
            className="dialog-backdrop"
            role="presentation"
            onMouseDown={() => setFailure(null)}
          >
            <section
              className="failure-dialog"
              role="dialog"
              aria-modal="true"
              aria-labelledby="failure-title"
              onMouseDown={(e) => e.stopPropagation()}
            >
              <div className="dialog-head">
                <div>
                  <span className="failure-icon">
                    <CircleAlert size={17} />
                  </span>
                  <h2 id="failure-title">任务失败原因</h2>
                </div>
                <button
                  className="dialog-close"
                  title="关闭"
                  aria-label="关闭"
                  onClick={() => setFailure(null)}
                >
                  <X size={18} />
                </button>
              </div>
              <p className="dialog-time">
                开始时间：{date(failure.started_at)}（北京时间）
              </p>
              <pre>{failure.error_message || "该任务未记录具体失败原因。"}</pre>
            </section>
          </div>
        )}
        {notice && <div className="toast">{notice}</div>}
      </section>
    </main>
  );
}
