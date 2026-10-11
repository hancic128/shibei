import i18n from "@/i18n";
import type {
  AppConfig,
  MeResponse,
  ReportData,
  ReportLangConfig,
  ReportsResponse,
  RunResponse,
  ScheduleConfig,
  Summary,
  TaskDetailResponse,
  TaskItem,
  TaskListResponse,
  WebhookConfig,
} from "@/types";

/** 浏览器侧 API 前缀：依赖 vite base（/shibei/api 或 /api） */
export const API_BASE = `${import.meta.env.BASE_URL}api`;

export class ApiError extends Error {
  status: number;
  code: string;
  friendly: boolean;
  /** 服务端附带的原始错误细节（如 Webhook 接收端返回的 HTTP 状态与响应体） */
  detail: string;

  constructor(message: string, status: number, code = "", friendly = false, detail = "") {
    super(message);
    this.status = status;
    this.code = code;
    this.friendly = friendly;
    this.detail = detail;
  }
}

/** 未登录（401）时抛出，由调用方统一跳登录 */
export class AuthError extends Error {
  constructor() {
    super("unauthorized");
  }
}

/** 网络层失败：友好文案（跟随界面语言），禁止裸显 Failed to fetch（规范 08） */
function friendlyMessage(e: unknown): string {
  if (e instanceof TypeError) return i18n.t("errors.network");
  return i18n.t("errors.server");
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let resp: Response;
  try {
    resp = await fetch(`${API_BASE}${path}`, {
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      ...init,
    });
  } catch (e) {
    throw new ApiError(friendlyMessage(e), 0, "network");
  }

  let body: unknown = null;
  try {
    body = await resp.json();
  } catch {
    // 非 JSON 响应
  }

  if (resp.status === 401) {
    // 会话过期/未登录：通知全局登出
    window.dispatchEvent(new Event("shibei:unauthorized"));
    throw new AuthError();
  }
  if (!resp.ok) {
    const payload = body as { error?: string; message?: string; detail?: string } | null;
    throw new ApiError(
      payload?.message || friendlyMessage(null),
      resp.status,
      payload?.error || "",
      false,
      payload?.detail || "",
    );
  }
  return body as T;
}

export const api = {
  me: () => request<MeResponse>("/me"),

  login: (username: string, password: string) =>
    request<MeResponse>("/login", { method: "POST", body: JSON.stringify({ username, password }) }),

  logout: () => request<{ ok: boolean }>("/logout", { method: "POST" }),

  getConfig: () => request<AppConfig>("/config"),

  saveConfig: (payload: {
    llm?: { base_url?: string; model?: string; max_tokens?: number };
    api_key?: string;
    clear_api_key?: boolean;
  }) =>
    request<{ ok: boolean }>("/config", {
      method: "PUT",
      body: JSON.stringify(payload),
    }),

  testConfig: (payload: { llm?: { base_url?: string; model?: string }; api_key?: string }) =>
    request<{ ok: boolean; latency_ms: number }>("/config/test", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  changePassword: (current_password: string, new_password: string) =>
    request<{ ok: boolean }>("/password", {
      method: "POST",
      body: JSON.stringify({ current_password, new_password }),
    }),

  setSource: (name: string, enabled: boolean) =>
    request<{ ok: boolean }>(`/sources/${encodeURIComponent(name)}`, {
      method: "PUT",
      body: JSON.stringify({ enabled }),
    }),

  schedule: () => request<ScheduleConfig>("/schedule"),

  saveSchedule: (payload: { enabled?: boolean; time?: string }) =>
    request<ScheduleConfig>("/schedule", {
      method: "PUT",
      body: JSON.stringify(payload),
    }),

  webhook: () => request<WebhookConfig>("/webhook"),

  saveWebhook: (payload: {
    enabled?: boolean;
    url?: string;
    header_key?: string;
    token?: string;
    clear_token?: boolean;
  }) =>
    request<WebhookConfig>("/webhook", {
      method: "PUT",
      body: JSON.stringify(payload),
    }),

  testWebhook: () => request<{ ok: boolean }>("/webhook/test", { method: "POST" }),

  stopTask: (id: string) =>
    request<{ ok: boolean }>(`/tasks/${encodeURIComponent(id)}/stop`, { method: "POST" }),

  run: (mode: "today" | "full" | "date", lang: "zh" | "en", date?: string) =>
    request<RunResponse>("/run", {
      method: "POST",
      body: JSON.stringify({ mode, lang, date }),
    }),

  tasks: () => request<TaskListResponse>("/tasks"),

  task: (id: string) => request<TaskDetailResponse>(`/tasks/${encodeURIComponent(id)}`),

  reports: () => request<ReportsResponse>("/reports"),

  report: (name: string, lang: "zh" | "en" = "zh") =>
    request<ReportData>(`/reports/${encodeURIComponent(name)}?lang=${lang}`),

  /** 删除某份报告（该日期的中英文版本一并移除，需登录） */
  deleteReport: (name: string) =>
    request<{ ok: boolean; removed: string[] }>(`/reports/${encodeURIComponent(name)}`, { method: "DELETE" }),

  reportLang: () => request<ReportLangConfig>("/report-lang"),

  saveReportLang: (lang: "zh" | "en") =>
    request<ReportLangConfig>("/report-lang", {
      method: "PUT",
      body: JSON.stringify({ lang }),
    }),

  /** 管理仪表数据（需登录） */
  summary: () => request<{ summary: Summary }>("/summary"),
};

export type { TaskItem };

/** 把界面语言同步为服务端「报告语言」，供定时任务（无浏览器上下文）沿用；未登录/失败静默忽略 */
export async function syncReportLang(lang: "zh" | "en"): Promise<void> {
  try {
    const current = await api.reportLang();
    if (current.lang !== lang) await api.saveReportLang(lang);
  } catch {
    // 公开页未登录或网络失败：不影响界面
  }
}
