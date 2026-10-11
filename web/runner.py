"""拾贝 Web 任务运行器。

以子进程方式运行 analyzer.py（与 CLI 完全一致的行为），管理任务状态、
日志文件与历史索引。同一时刻只允许一个任务运行（社区抓取与分析都较慢，
并发无意义且容易互相踩 state.json）。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

MAX_KEPT_TASKS = 30  # 索引与日志最多保留的任务数

logger = logging.getLogger("shibei.runner")


class TaskRunningError(Exception):
    """已有任务在运行，拒绝并发触发。"""


def _now() -> float:
    return time.time()


class TaskManager:
    """管理 analyzer 子进程任务的生命周期。"""

    def __init__(
        self,
        repo_root: Path,
        on_finish: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self._on_finish = on_finish
        self.tasks_dir = self.repo_root / "data" / "tasks"
        self.index_file = self.tasks_dir / "index.json"
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._tasks: dict[str, dict[str, Any]] = {}
        self._procs: dict[str, subprocess.Popen[Any]] = {}
        self._load_index()
        self._mark_interrupted_on_boot()

    # ---------- 索引持久化 ----------

    def _load_index(self) -> None:
        if self.index_file.exists():
            try:
                data = json.loads(self.index_file.read_text(encoding="utf-8"))
                self._tasks = {k: v for k, v in data.get("tasks", {}).items()}
            except (OSError, ValueError):
                self._tasks = {}
        # 引导期把遗留 running 标为 interrupted（进程已不在，无法续跑）
        for task in self._tasks.values():
            if task.get("status") == "running":
                task["status"] = "interrupted"
                task["finished_at"] = _now()

    def _save_index(self) -> None:
        self.index_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.index_file.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"tasks": self._tasks}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.index_file)

    def _prune(self) -> None:
        """只保留最近 MAX_KEPT_TASKS 条，并清理被淘汰任务的日志文件。"""
        if len(self._tasks) <= MAX_KEPT_TASKS:
            return
        ordered = sorted(self._tasks.values(), key=lambda t: t.get("started_at", 0))
        for task in ordered[: len(self._tasks) - MAX_KEPT_TASKS]:
            tid = task["id"]
            log = self.tasks_dir / f"{tid}.log"
            log.unlink(missing_ok=True)
            self._tasks.pop(tid, None)

    def _mark_interrupted_on_boot(self) -> None:
        # 若上次运行被强杀/容器重启，正在 running 的任务在 _load_index 已标记
        if any(t.get("status") == "interrupted" for t in self._tasks.values()):
            self._save_index()

    # ---------- 查询 ----------

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            task = self._tasks.get(task_id)
            return dict(task) if task else None

    def list(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            tasks = sorted(self._tasks.values(), key=lambda t: t.get("started_at", 0), reverse=True)
            return [dict(t) for t in tasks[:limit]]

    def running(self) -> dict[str, Any] | None:
        with self._lock:
            for task in self._tasks.values():
                if task.get("status") == "running":
                    return dict(task)
            return None

    # ---------- 启动 / 完成 ----------

    def start(self, mode: str, env: dict[str, str], date: str | None = None) -> dict[str, Any]:
        """启动一个 analyzer 子进程任务。

        mode: "today"（增量，默认行为）、"full"（强制全量）或 "date"（按日期历史重算，需 date）。
        env: 供 analyzer 使用的环境变量（已含 OPENAI_* 配置）。
        返回任务快照；已有任务运行中抛 TaskRunningError。
        """
        if mode not in ("today", "full", "date"):
            raise ValueError(f"未知模式: {mode}")
        if mode == "date" and not date:
            raise ValueError("date 模式必须提供日期")
        with self._lock:
            if any(t.get("status") == "running" for t in self._tasks.values()):
                raise TaskRunningError("已有分析任务在运行")

            task_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
            task: dict[str, Any] = {
                "id": task_id,
                "mode": mode,
                "date": date,
                "lang": env.get("ANALYZE_LANG", "zh"),
                "status": "running",
                "started_at": _now(),
                "finished_at": None,
                "exit_code": None,
                "log_file": str(self.tasks_dir / f"{task_id}.log"),
            }
            self._tasks[task_id] = task
            self._prune()
            self._save_index()

        log_fh = open(self.tasks_dir / f"{task_id}.log", "w", encoding="utf-8")
        extra_args = ["--full"] if mode == "full" else (["--date", date] if mode == "date" and date else [])
        proc = subprocess.Popen(
            [sys.executable, "-u", os.fspath(self.repo_root / "analyzer.py")] + extra_args,
            cwd=self.repo_root,
            env=env,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            text=True,
        )
        with self._lock:
            self._procs[task_id] = proc

        def _wait() -> None:
            try:
                exit_code = proc.wait()
            except Exception:  # 极端情况下 wait 失败也按失败落账
                exit_code = 1
            finally:
                log_fh.close()
            with self._lock:
                t = self._tasks.get(task_id)
                stopped = bool(t and t.get("stop_requested"))
                if t:
                    # SIGTERM（stop/中断）→ interrupted；exit 0 → succeeded；其余 → failed
                    if stopped or exit_code < 0:
                        t["status"] = "interrupted"
                    else:
                        t["status"] = "succeeded" if exit_code == 0 else "failed"
                    t["exit_code"] = exit_code
                    t["finished_at"] = _now()
                    self._save_index()
                self._procs.pop(task_id, None)
            # 任务结束回调（Webhook 通知等），在守护线程中执行
            snapshot = dict(t) if t else {}
            if snapshot and self._on_finish:
                try:
                    self._on_finish(snapshot)
                except Exception:
                    logger.exception("任务完成回调异常")

        threading.Thread(target=_wait, daemon=True, name=f"task-{task_id}").start()
        return dict(task)

    # ---------- 停止 ----------

    def stop(self, task_id: str) -> bool:
        """请求停止运行中的任务（SIGTERM 子进程，状态落 interrupted）。返回是否成功发起。"""
        with self._lock:
            task = self._tasks.get(task_id)
            proc = self._procs.get(task_id)
            if not task or task.get("status") != "running":
                return False
            task["stop_requested"] = True
        if proc:
            try:
                proc.terminate()
            except OSError:
                pass
        return True

    # ---------- 日志 ----------

    def log_tail(self, task_id: str, max_chars: int = 3000) -> str:
        log_file = self.tasks_dir / f"{task_id}.log"
        if not log_file.exists():
            return ""
        try:
            content = log_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return content[-max_chars:]

    def full_log(self, task_id: str) -> str:
        log_file = self.tasks_dir / f"{task_id}.log"
        if not log_file.exists():
            return ""
        try:
            return log_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
