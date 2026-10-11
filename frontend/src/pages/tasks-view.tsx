import { FileText, History, Loader2, Square } from "lucide-react";
import type { TFunction } from "i18next";
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody } from "@/components/ui/card";
import { ConfirmDialog } from "@/components/ui/confirm";
import { Modal } from "@/components/ui/dialog";
import { EmptyState } from "@/components/ui/empty";
import { ErrorBlock } from "@/components/ui/error";
import { useToast } from "@/components/ui/toast";
import { PageHeader } from "@/components/layout/page-header";
import { usePolling, useTimezone } from "@/hooks/usePrefs";
import { api } from "@/lib/api";
import { formatDuration, formatTime } from "@/lib/time";
import type { TaskItem } from "@/types";

function statusBadge(task: TaskItem, t: TFunction) {
  switch (task.status) {
    case "running":
      return (
        <Badge variant="brand" icon={<Loader2 className="h-3 w-3 animate-spin" aria-hidden="true" />}>
          {t("tasks.statusRunning")}
        </Badge>
      );
    case "succeeded":
      return (
        <Badge variant="success" dot>
          {t("tasks.statusSucceeded")}
        </Badge>
      );
    case "failed":
      return (
        <Badge variant="danger" dot>
          {t("tasks.statusFailed")}
        </Badge>
      );
    default:
      return (
        <Badge variant="neutral" dot>
          {t("tasks.statusInterrupted")}
        </Badge>
      );
  }
}

/** 运行历史页（布局 4.4 标题+表格）：任务列表 + 日志查看 */
export function TasksView() {
  const { t } = useTranslation();
  const toast = useToast();
  const tz = useTimezone();

  const [tasks, setTasks] = useState<TaskItem[] | null>(null);
  const [error, setError] = useState(false);
  const [logTask, setLogTask] = useState<TaskItem | null>(null);
  const [stopTask, setStopTask] = useState<TaskItem | null>(null);
  const [stopping, setStopping] = useState(false);
  const [logContent, setLogContent] = useState<string | null>(null);
  const [logLoading, setLogLoading] = useState(false);
  const [logError, setLogError] = useState(false);
  const loadingRef = useRef(false);
  const hasDataRef = useRef(false);

  const load = useCallback(async () => {
    if (loadingRef.current) return;
    loadingRef.current = true;
    try {
      const data = await api.tasks();
      setTasks(data.tasks);
      hasDataRef.current = true;
      setError(false);
    } catch {
      if (!hasDataRef.current) setError(true);
    } finally {
      loadingRef.current = false;
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // 有运行中任务时轮询刷新
  const hasRunning = (tasks ?? []).some((x) => x.status === "running");
  usePolling(
    async () => {
      const data = await api.tasks();
      setTasks(data.tasks);
      return !data.tasks.some((x) => x.status === "running");
    },
    3000,
    hasRunning,
  );

  const openLog = async (task: TaskItem) => {
    setLogTask(task);
    setLogContent(null);
    setLogError(false);
    setLogLoading(true);
    try {
      const data = await api.task(task.id);
      setLogContent(data.task.log_tail ?? "");
      setLogLoading(false);
    } catch {
      setLogError(true);
      setLogLoading(false);
      toast.push("error", t("tasks.logLoadFailed"));
    }
  };

  return (
    <div className="space-y-6">
      <PageHeader title={t("tasks.title")} desc={t("tasks.desc")} />

      {error && !tasks ? (
        <ErrorBlock title={t("tasks.loadFailedTitle")} desc={t("tasks.loadFailedDesc")} onRetry={() => void load()} />
      ) : tasks === null ? (
        <div className="space-y-3 py-4" aria-busy="true">
          <div className="h-4 w-full animate-pulse rounded bg-surface-2 dark:bg-ink-900" />
          <div className="h-4 w-full animate-pulse rounded bg-surface-2 dark:bg-ink-900" />
        </div>
      ) : tasks.length === 0 ? (
        <Card>
          <CardBody>
            <EmptyState icon={History} title={t("tasks.noTasksTitle")} desc={t("tasks.noTasksDesc")} />
          </CardBody>
        </Card>
      ) : (
        <Card className="overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-surface-3 bg-surface-1 text-left text-xs font-medium text-ink-500 dark:border-ink-900 dark:bg-ink-900 dark:text-surface-4">
                  <th className="px-6 py-3 font-medium">{t("tasks.colStarted")}</th>
                  <th className="px-4 py-3 font-medium">{t("tasks.colMode")}</th>
                  <th className="px-4 py-3 font-medium">{t("tasks.colStatus")}</th>
                  <th className="px-4 py-3 font-medium">{t("tasks.colDuration")}</th>
                  <th className="px-6 py-3 text-right font-medium">{t("tasks.colAction")}</th>
                </tr>
              </thead>
              <tbody>
                {tasks.map((task) => (
                  <tr
                    key={task.id}
                    className="border-b border-surface-3 last:border-0 hover:bg-surface-1 dark:border-ink-900 dark:hover:bg-ink-900"
                  >
                    <td className="whitespace-nowrap px-6 py-3 text-ink-700 dark:text-surface-4">
                      {formatTime(task.started_at, tz)}
                    </td>
                    <td className="px-4 py-3">
                      <div className="flex items-center gap-1.5">
                        <Badge variant={task.mode === "full" ? "brand" : "neutral"}>
                          {task.mode === "full"
                            ? t("tasks.modeFull")
                            : task.mode === "date"
                              ? t("tasks.modeDate")
                              : t("tasks.modeToday")}
                        </Badge>
                        {task.lang && <Badge variant="neutral">{task.lang === "en" ? t("tasks.langEn") : t("tasks.langZh")}</Badge>}
                      </div>
                    </td>
                    <td className="px-4 py-3">{statusBadge(task, t)}</td>
                    <td className="whitespace-nowrap px-4 py-3 text-ink-500 dark:text-surface-4">
                      {formatDuration(task.started_at, task.finished_at)}
                    </td>
                    <td className="px-6 py-3 text-right">
                      <div className="flex items-center justify-end gap-1">
                        {task.status === "running" && (
                          <Button
                            variant="ghost"
                            size="sm"
                            icon={<Square className="h-4 w-4" aria-hidden="true" />}
                            className="text-rose-600 hover:text-rose-700 dark:text-rose-400 dark:hover:text-rose-300"
                            onClick={() => setStopTask(task)}
                          >
                            {t("tasks.stopLabel")}
                          </Button>
                        )}
                        <Button
                          variant="ghost"
                          size="sm"
                          icon={<FileText className="h-4 w-4" aria-hidden="true" />}
                          onClick={() => void openLog(task)}
                        >
                          {t("tasks.actionLog")}
                        </Button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      {/* 日志对话框 */}
      <Modal
        open={logTask != null}
        onOpenChange={(open) => {
          if (!open) setLogTask(null);
        }}
        title={`${t("tasks.logTitle")} · ${logTask?.id ?? ""}`}
        size="xl"
        footer={
          <Button variant="secondary" onClick={() => setLogTask(null)}>
            {t("common.close")}
          </Button>
        }
      >
        {logLoading ? (
          <div className="space-y-2 py-4" aria-busy="true">
            <div className="h-4 w-full animate-pulse rounded bg-surface-2 dark:bg-ink-900" />
            <div className="h-4 w-3/4 animate-pulse rounded bg-surface-2 dark:bg-ink-900" />
          </div>
        ) : logError ? (
          <ErrorBlock
            title={t("tasks.logLoadFailed")}
            desc={t("errors.server")}
            onRetry={() => logTask && void openLog(logTask)}
          />
        ) : logContent ? (
          <pre className="scrollbar-thin max-h-[50vh] overflow-auto whitespace-pre-wrap rounded-lg bg-surface-1 p-4 font-mono text-xs leading-5 text-ink-700 dark:bg-ink-900 dark:text-surface-4">
            {logContent}
          </pre>
        ) : (
          <p className="py-4 text-sm text-ink-500 dark:text-surface-4">{t("tasks.logEmpty")}</p>
        )}
      </Modal>

      {/* 停止任务确认 */}
      <ConfirmDialog
        open={stopTask != null}
        onOpenChange={(open) => {
          if (!open) setStopTask(null);
        }}
        title={t("tasks.stopConfirmTitle")}
        message={t("tasks.stopConfirmMessage")}
        confirmLabel={t("tasks.stopLabel")}
        danger
        confirmLoading={stopping}
        onConfirm={() => {
          if (!stopTask) return;
          setStopping(true);
          api
            .stopTask(stopTask.id)
            .then(() => {
              toast.push("success", t("tasks.stopSent"));
              setStopTask(null);
            })
            .catch(() => {
              toast.push("error", t("tasks.statusFailed"));
              setStopTask(null);
            })
            .finally(() => setStopping(false));
        }}
      />
    </div>
  );
}
