/** 后端 API 数据模型 */

export interface LlmConfig {
  base_url: string;
  model: string;
  max_tokens: number;
  has_api_key: boolean;
  api_key_hint: string;
}

export interface SourceItem {
  name: string;
  enabled: boolean;
  nodes: string[];
}

export interface AppConfig {
  llm: LlmConfig;
  sources: SourceItem[];
}

export interface TaskItem {
  id: string;
  mode: "today" | "full" | "date";
  /** mode="date" 时重算的目标日期（YYYY-MM-DD），其余为 null */
  date?: string | null;
  lang?: "zh" | "en";
  status: "running" | "succeeded" | "failed" | "interrupted";
  started_at: number | null;
  finished_at: number | null;
  exit_code: number | null;
  log_tail?: string;
}

export interface TaskListResponse {
  tasks: TaskItem[];
}

export interface TaskDetailResponse {
  task: TaskItem;
}

export interface RunResponse {
  task: TaskItem;
}

export interface ReportMeta {
  name: string;
  kind: "daily" | "full";
  lang: "zh" | "en";
  file: string;
  updated_at: number | null;
  size: number;
}

export interface ReportData {
  name: string;
  language: "zh" | "en";
  updated_at: number | null;
  content: string;
}

export interface ReportLangConfig {
  lang: "zh" | "en";
}

export interface Summary {
  total_posts: number;
  per_source: Record<string, number>;
  llm_configured: boolean;
  has_full_report: boolean;
  has_today_report: boolean;
  /** 最新一份每日归档的日期（YYYY-MM-DD），无则 null；用于默认定位 */
  latest_daily: string | null;
}

export interface ReportsResponse {
  reports: ReportMeta[];
}

export interface MeResponse {
  username: string;
}

export interface ScheduleConfig {
  enabled: boolean;
  time: string; // HH:MM
  last_fired?: string | null;
}

export interface WebhookConfig {
  enabled: boolean;
  url: string;
  header_key: string;
  has_token: boolean;
  token_hint: string;
}

export type ViewId = "reports" | "tasks" | "settings" | "help";
