"""拾贝 · Web 界面后端（Flask）。

职责：
- 认证：登录 / 退出（flask signed session，凭据 scrypt 哈希持久化）
- 配置：LLM（base_url / model / max_tokens / api_key）与来源启用开关
- 任务：触发 analyzer 子进程（增量 / 全量）、查询状态与日志
- 报告：列出 / 读取 data/analysis 下的 Markdown 报告
- 静态：托管前端构建产物（frontend/dist → /app/static）

运行配置（环境变量）：
    SHIBEI_PORT        监听端口（默认 8000）
    SHIBEI_BASE_PATH   浏览器侧访问前缀，如 /shibei/（决定 cookie path）
    SHIBEI_USERNAME    初始管理员用户名（默认 admin）
    SHIBEI_PASSWORD    初始管理员密码；缺省时自动生成并打印到日志

运行时数据全部落在 data/ 下（已 gitignore）：
    data/web_config.json    运行时配置权威（含 sources 与 llm 节）
    data/web_secrets.json    登录凭据哈希与 LLM API Key（0600）
    data/.secret_key         Flask session 签名密钥
    data/tasks/              任务索引与日志
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from collections.abc import Callable
from datetime import timedelta
from functools import wraps
from pathlib import Path
from typing import Any, TypeVar

from flask import Flask, jsonify, request, send_from_directory, session
from waitress import serve

from web import settings as web_settings
from web.runner import TaskManager, TaskRunningError
from web.scheduler import DailyScheduler
from web.webhook import send_task_finished, send_test

logger = logging.getLogger("shibei.web")

TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class ApiKeyMissingError(Exception):
    """未配置 LLM API Key，无法启动分析任务。"""


REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
REPORT_DIR = DATA_DIR / "analysis"
CONFIG_FILE = REPO_ROOT / "config.json"
WEB_CONFIG_FILE = DATA_DIR / "web_config.json"
SECRETS_FILE = DATA_DIR / "web_secrets.json"
SECRET_KEY_FILE = DATA_DIR / ".secret_key"
STATIC_DIR = REPO_ROOT / "static"
VERSION_FILE = REPO_ROOT / "VERSION"

# 报告文件命名：每日归档 YYYY-MM-DD.md（增量，每天一份）+ analysis.md（全量总览）；
# 英文报告在基名后加 .en 后缀（analysis.en.md / YYYY-MM-DD.en.md），zh 无后缀向后兼容。
DAILY_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
FULL_REPORT_NAME = "analysis"
REPORT_FILE_RE = re.compile(r"^(analysis|\d{4}-\d{2}-\d{2})(?:\.(zh|en))?$")


def _valid_daily_date(name: str) -> bool:
    """严格校验 YYYY-MM-DD 且为真实日历日（拒绝 2024-02-30 这类）。"""
    if not DAILY_NAME_RE.fullmatch(name):
        return False
    try:
        time.strptime(name, "%Y-%m-%d")
    except ValueError:
        return False
    return True


SESSION_TTL_DAYS = 7
F = TypeVar("F", bound=Callable[..., Any])


# ---------- 持久化小工具 ----------


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path: Path, obj: Any, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    if mode is not None:
        tmp.chmod(mode)
    tmp.replace(path)


def _atomic_write_text(path: Path, text: str, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    if mode is not None:
        tmp.chmod(mode)
    tmp.replace(path)


# ---------- 凭据与密钥 ----------


def _ensure_secret_key() -> str:
    if SECRET_KEY_FILE.exists():
        key = SECRET_KEY_FILE.read_text(encoding="utf-8").strip()
        if key:
            return key
    key = secrets.token_urlsafe(48)
    _atomic_write_text(SECRET_KEY_FILE, key, mode=0o600)
    return key


def _hash_password(password: str) -> str:
    """scrypt 哈希，格式: scrypt$<salt_b64>$<hash_b64>。"""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def _verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt_b64, hash_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
    except (ValueError, TypeError):
        return False
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
    return hmac.compare_digest(digest, expected)


class Secrets:
    """web_secrets.json：{username, password_hash, llm_api_key}。"""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        self._data = _read_json(SECRETS_FILE, {})
        if not self._data:
            self._bootstrap()

    def _bootstrap(self) -> None:
        username = os.environ.get("SHIBEI_USERNAME", "").strip() or "admin"
        password = os.environ.get("SHIBEI_PASSWORD", "").strip()
        generated = False
        if not password:
            password = secrets.token_urlsafe(12)
            generated = True
        self._data = {
            "username": username,
            "password_hash": _hash_password(password),
            "llm_api_key": "",
        }
        self.save()
        if generated:
            # 自动生成的初始密码打印到容器日志，仅供首次登录
            print(f"[shibei] 未设置 SHIBEI_PASSWORD，已生成初始管理员密码：{password}", flush=True)

    @property
    def username(self) -> str:
        return self._data.get("username", "")

    @property
    def has_llm_api_key(self) -> bool:
        return bool(self._data.get("llm_api_key"))

    @property
    def llm_api_key(self) -> str:
        return self._data.get("llm_api_key", "")

    def verify_login(self, username: str, password: str) -> bool:
        ok_user = username == self._data.get("username")
        return ok_user and _verify_password(password, self._data.get("password_hash", ""))

    def set_llm_api_key(self, api_key: str) -> None:
        self._data["llm_api_key"] = api_key
        self.save()

    def change_password(self, username: str, password: str) -> None:
        self._data["username"] = username
        self._data["password_hash"] = _hash_password(password)
        self.save()

    def save(self) -> None:
        _write_json(SECRETS_FILE, self._data, mode=0o600)


class WebConfig:
    """运行时配置权威（data/web_config.json），结构与仓库 config.json 一致。

    web 修改先落 web_config，再同步写仓库 config.json，保证 CLI 与容器内
    直接运行 analyzer.py 读到的是同一份最新配置。
    """

    def __init__(self) -> None:
        if not WEB_CONFIG_FILE.exists():
            default = _read_json(CONFIG_FILE, {"sources": {}, "llm": {}})
            _write_json(WEB_CONFIG_FILE, default, mode=0o600)
        self._data = _read_json(WEB_CONFIG_FILE, {"sources": {}, "llm": {}})
        self._sync_repo_config()

    def _sync_repo_config(self) -> None:
        # 只在内容确有差异时写，避免无谓触碰仓库文件
        if _read_json(CONFIG_FILE, None) != self._data:
            _write_json(CONFIG_FILE, self._data)

    def get(self) -> dict[str, Any]:
        return json.loads(json.dumps(self._data))

    def llm(self) -> dict[str, Any]:
        llm = self._data.setdefault("llm", {})
        return {
            "base_url": str(llm.get("base_url", "")),
            "model": str(llm.get("model", "")),
            "max_tokens": int(llm.get("max_tokens", 4096) or 4096),
        }

    def sources(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for name, conf in self._data.get("sources", {}).items():
            out.append(
                {
                    "name": name,
                    "enabled": bool(conf.get("enabled", True)),
                    "nodes": list(conf.get("nodes", [])),
                }
            )
        return out

    def set_llm(self, llm: dict[str, Any]) -> None:
        target = self._data.setdefault("llm", {})
        if "base_url" in llm:
            target["base_url"] = llm["base_url"].rstrip("/")
        if "model" in llm:
            target["model"] = llm["model"]
        if "max_tokens" in llm:
            target["max_tokens"] = int(llm["max_tokens"])
        self._save()

    def set_source_enabled(self, name: str, enabled: bool) -> None:
        conf = self._data.setdefault("sources", {}).setdefault(name, {})
        conf["enabled"] = bool(enabled)
        self._save()

    def _save(self) -> None:
        _write_json(WEB_CONFIG_FILE, self._data, mode=0o600)
        self._sync_repo_config()


# ---------- 报告 ----------


def _report_list() -> list[dict[str, Any]]:
    """全部可浏览报告：每日归档（YYYY-MM-DD，新在前）+ 全量总览 analysis[.en]。

    只认以上两类命名；analysis_today.md 等旧命名忽略（部署迁移时已归档）。
    name 为基名（analysis / YYYY-MM-DD），lang 标记文件语言（zh 无后缀 / en 带 .en）。
    """
    dailies: list[dict[str, Any]] = []
    full_items: list[dict[str, Any]] = []
    for path in REPORT_DIR.glob("*.md"):
        m = REPORT_FILE_RE.fullmatch(path.stem)
        if not m:
            continue
        base, lang = m.group(1), m.group(2) or "zh"
        kind = "full" if base == FULL_REPORT_NAME else "daily"
        try:
            stat = path.stat()
        except OSError:
            continue  # 与删除并发：文件已消失，跳过
        item = {
            "name": base,
            "kind": kind,
            "lang": lang,
            "file": path.name,
            "updated_at": stat.st_mtime,
            "size": stat.st_size,
        }
        (full_items if kind == "full" else dailies).append(item)
    dailies.sort(key=lambda r: r["name"], reverse=True)
    return dailies + sorted(full_items, key=lambda r: r["lang"])


def _summary(config: WebConfig, secrets_store: Secrets) -> dict[str, Any]:
    per_source: dict[str, int] = {}
    total = 0
    if DATA_DIR.is_dir():
        excluded = (".cache", ".tasks", "analysis", "tasks")
        for source_dir in sorted(p for p in DATA_DIR.iterdir() if p.is_dir() and p.name not in excluded):
            count = sum(1 for _ in source_dir.rglob("*.json") if not _.name.endswith(".tmp"))
            if count:
                per_source[source_dir.name] = count
            total += count
    reports = {r["name"]: r for r in _report_list()}
    dailies = [r["name"] for r in _report_list() if r["kind"] == "daily"]
    return {
        "total_posts": total,
        "per_source": per_source,
        "llm_configured": bool(config.llm()["base_url"] and config.llm()["model"] and secrets_store.has_llm_api_key),
        "has_full_report": FULL_REPORT_NAME in reports,
        "has_today_report": time.strftime("%Y-%m-%d") in reports,
        "latest_daily": dailies[0] if dailies else None,
    }


# ---------- Flask 应用 ----------


def create_app() -> Flask:
    base_path = os.environ.get("SHIBEI_BASE_PATH", "/shibei/").strip()
    if not base_path.startswith("/"):
        base_path = "/" + base_path
    if not base_path.endswith("/"):
        base_path += "/"

    # dist 结构：index.html + assets/；/assets 映射到 static/assets 目录
    app = Flask(__name__, static_folder=str(STATIC_DIR / "assets"), static_url_path="/assets")
    app.config.update(
        SECRET_KEY=_ensure_secret_key(),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_PATH=base_path,
        PERMANENT_SESSION_LIFETIME=timedelta(days=SESSION_TTL_DAYS),
    )

    secrets_store = Secrets()
    config = WebConfig()
    tasks = TaskManager(REPO_ROOT, on_finish=send_task_finished)

    @app.after_request
    def _no_cache_api(response: Any) -> Any:
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    # ---------- 认证 ----------

    def require_login(fn: F) -> F:
        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if not session.get("user"):
                return jsonify({"error": "unauthorized", "message": "请先登录"}), 401
            return fn(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    def require_same_origin(fn: F) -> F:
        """JSON 写接口的 CSRF 防线：校验 Origin / Referer 与本机一致。"""

        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            origin = request.headers.get("Origin") or request.headers.get("Referer")
            if origin:
                from urllib.parse import urlparse

                # 忽略端口（nginx 反代时 Host 不带端口）；hostname 不同即视为跨站
                origin_host = (urlparse(origin).hostname or "").lower()
                host = (request.host.split(":")[0]).lower()
                if origin_host != host:
                    return jsonify({"error": "bad_origin", "message": "请求来源不合法"}), 403
            return fn(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    def body_json() -> dict[str, Any]:
        data = request.get_json(silent=True)
        return data if isinstance(data, dict) else {}

    @app.post("/api/login")
    @require_same_origin
    def login() -> Any:
        data = body_json()
        username = str(data.get("username", "")).strip()
        password = str(data.get("password", ""))
        # 轻量防爆破：失败固定延迟，不区分“用户不存在/密码错”
        time.sleep(0.4)
        if not secrets_store.verify_login(username, password):
            return jsonify({"error": "bad_credentials", "message": "用户名或密码错误"}), 401
        session.permanent = True
        session["user"] = username
        return jsonify({"username": username})

    @app.post("/api/logout")
    @require_same_origin
    def logout() -> Any:
        session.clear()
        return jsonify({"ok": True})

    @app.get("/api/me")
    def me() -> Any:
        user = session.get("user")
        if not user:
            return jsonify({"error": "unauthorized", "message": "请先登录"}), 401
        return jsonify({"username": user})

    # ---------- 配置 ----------

    def _mask_key(key: str) -> str:
        if not key:
            return ""
        return "••••" + key[-4:]

    @app.get("/api/config")
    @require_login
    def get_config() -> Any:
        llm = config.llm()
        llm_view = {
            **llm,
            "has_api_key": secrets_store.has_llm_api_key,
            "api_key_hint": _mask_key(secrets_store.llm_api_key),
        }
        return jsonify({"llm": llm_view, "sources": config.sources()})

    @app.post("/api/config/test")
    @require_login
    @require_same_origin
    def test_llm() -> Any:
        """用界面当前表单（或已保存配置）发起一次最小对话，验证连通性。"""
        data = body_json()
        llm_in = data.get("llm")
        base_url = ""
        model = ""
        if isinstance(llm_in, dict):
            base_url = str(llm_in.get("base_url", "")).strip()
            model = str(llm_in.get("model", "")).strip()
        saved = config.llm()
        base_url = base_url or saved["base_url"]
        model = model or saved["model"]
        if not base_url.startswith(("http://", "https://")) or not model:
            return jsonify({"error": "bad_config", "message": "请先填写接口地址与模型名"}), 400
        env_key = os.environ.get("OPENAI_API_KEY", "")
        api_key = str(data.get("api_key", "")).strip() or secrets_store.llm_api_key or env_key
        if not api_key:
            return jsonify({"error": "no_api_key", "message": "未配置 API Key"}), 400

        from analyzer import _LLM, call_api  # 延迟导入，复用错误透传与重试

        saved_key = os.environ.get("OPENAI_API_KEY")
        os.environ["OPENAI_API_KEY"] = api_key
        old_llm = dict(_LLM)
        _LLM.update({"base_url": base_url.rstrip("/"), "model": model, "max_tokens": "256"})
        started = time.time()
        try:
            call_api("请直接回复：ok", timeout=30, retries=0)
            return jsonify({"ok": True, "model": model, "latency_ms": int((time.time() - started) * 1000)})
        except RuntimeError as e:
            return jsonify({"error": "llm_test_failed", "message": str(e)}), 400
        finally:
            _LLM.clear()
            _LLM.update(old_llm)
            if saved_key is None:
                os.environ.pop("OPENAI_API_KEY", None)
            else:
                os.environ["OPENAI_API_KEY"] = saved_key

    @app.put("/api/config")
    @require_login
    @require_same_origin
    def put_config() -> Any:
        data = body_json()
        llm_in = data.get("llm")
        if isinstance(llm_in, dict):
            patch: dict[str, Any] = {}
            base_url = str(llm_in.get("base_url", "")).strip()
            model = str(llm_in.get("model", "")).strip()
            if "base_url" in llm_in:
                if not base_url.startswith(("http://", "https://")):
                    return jsonify({"error": "bad_url", "message": "API 地址需以 http:// 或 https:// 开头"}), 400
                patch["base_url"] = base_url
            if "model" in llm_in:
                if not model:
                    return jsonify({"error": "bad_model", "message": "模型名不能为空"}), 400
                patch["model"] = model
            if "max_tokens" in llm_in:
                try:
                    patch["max_tokens"] = max(256, int(llm_in["max_tokens"]))
                except (TypeError, ValueError):
                    return jsonify({"error": "bad_tokens", "message": "max_tokens 需为数字"}), 400
            if patch:
                config.set_llm(patch)

        api_key = data.get("api_key")
        if isinstance(api_key, str) and api_key.strip():
            secrets_store.set_llm_api_key(api_key.strip())
        elif data.get("clear_api_key") is True:
            secrets_store.set_llm_api_key("")

        return jsonify({"ok": True})

    @app.post("/api/password")
    @require_login
    @require_same_origin
    def change_password() -> Any:
        data = body_json()
        current = str(data.get("current_password", ""))
        new = str(data.get("new_password", ""))
        if not secrets_store.verify_login(session["user"], current):
            return jsonify({"error": "bad_credentials", "message": "当前密码不正确"}), 401
        if len(new) < 8:
            return jsonify({"error": "weak_password", "message": "新密码至少 8 位"}), 400
        secrets_store.change_password(secrets_store.username, new)
        return jsonify({"ok": True})

    @app.put("/api/sources/<name>")
    @require_login
    @require_same_origin
    def set_source(name: str) -> Any:
        data = body_json()
        config.set_source_enabled(name, bool(data.get("enabled", True)))
        return jsonify({"ok": True})

    # ---------- 任务 ----------

    def _run_env(lang: str | None = None) -> dict[str, str]:
        env = dict(os.environ)
        llm = config.llm()
        if llm["base_url"]:
            env["OPENAI_BASE_URL"] = llm["base_url"]
        if llm["model"]:
            env["ANALYZE_MODEL"] = llm["model"]
        env["ANALYZE_MAX_TOKENS"] = str(llm["max_tokens"])
        # 报告/日志语言：调用方传入（界面语言）优先，否则用服务端记住的语言（定时任务）
        env["ANALYZE_LANG"] = lang or web_settings.load_report_lang()
        if secrets_store.llm_api_key:
            env["OPENAI_API_KEY"] = secrets_store.llm_api_key
        return env

    def _start_task(mode: str, lang: str | None = None, date: str | None = None) -> dict[str, Any]:
        """组装环境并启动任务；未配 Key 抛 ApiKeyMissingError，运行中抛 TaskRunningError。"""
        env = _run_env(lang)
        if not env.get("OPENAI_API_KEY"):
            raise ApiKeyMissingError("尚未配置 LLM API Key")
        return tasks.start(mode, env, date)

    @app.post("/api/run")
    @require_login
    @require_same_origin
    def run_task() -> Any:
        data = body_json()
        mode = data.get("mode", "today")
        if mode not in ("today", "full", "date"):
            return jsonify({"error": "bad_mode", "message": "mode 需为 today、full 或 date"}), 400
        # date 模式：按日期历史重算该天报告，需合法 YYYY-MM-DD
        date = str(data.get("date", "")).strip()
        if mode == "date" and not _valid_daily_date(date):
            return jsonify({"error": "bad_date", "message": "date 需为合法的 YYYY-MM-DD"}), 400
        # lang 可选：调用方界面语言；非法值拒绝，合法值记住（定时任务无浏览器上下文，沿用它）
        lang = str(data.get("lang", "")).strip()
        if lang and lang not in web_settings.REPORT_LANGS:
            return jsonify({"error": "bad_lang", "message": "lang 需为 zh 或 en"}), 400
        if lang:
            web_settings.save_report_lang(lang)
        try:
            task = _start_task(mode, lang or None, date or None)
        except ApiKeyMissingError:
            return jsonify({"error": "no_api_key", "message": "尚未配置 LLM API Key，请先到「设置」完成 AI 配置"}), 400
        except TaskRunningError as e:
            running = tasks.running()
            return (
                jsonify({"error": "task_running", "message": str(e), "running": running}),
                409,
            )
        return jsonify({"task": task}), 202

    @app.get("/api/tasks")
    @require_login
    def list_tasks() -> Any:
        return jsonify({"tasks": tasks.list(limit=20)})

    @app.get("/api/tasks/<task_id>")
    @require_login
    def task_detail(task_id: str) -> Any:
        task = tasks.get(task_id)
        if not task:
            return jsonify({"error": "not_found", "message": "任务不存在"}), 404
        return jsonify({"task": {**task, "log_tail": tasks.log_tail(task_id, max_chars=4000)}})

    @app.post("/api/tasks/<task_id>/stop")
    @require_login
    @require_same_origin
    def task_stop(task_id: str) -> Any:
        if not tasks.get(task_id):
            return jsonify({"error": "not_found", "message": "任务不存在"}), 404
        if not tasks.stop(task_id):
            return jsonify({"error": "not_running", "message": "任务不在运行中"}), 409
        return jsonify({"ok": True})

    # ---------- 每日定时调度 ----------

    @app.get("/api/schedule")
    @require_login
    def schedule_get() -> Any:
        cfg = web_settings.load_schedule()
        return jsonify(
            {
                "enabled": bool(cfg.get("enabled")),
                "time": str(cfg.get("time", "00:00")),
                "last_fired": cfg.get("last_fired"),
            }
        )

    @app.put("/api/schedule")
    @require_login
    @require_same_origin
    def schedule_put() -> Any:
        data = body_json()
        time_value = data.get("time")
        if time_value is not None:
            if not isinstance(time_value, str) or not TIME_RE.match(time_value):
                return jsonify({"error": "bad_time", "message": "时间格式需为 HH:MM"}), 400
        enabled = data.get("enabled")
        if enabled is not None and not isinstance(enabled, bool):
            return jsonify({"error": "bad_enabled", "message": "enabled 需为布尔值"}), 400
        saved = web_settings.save_schedule(
            enabled=bool(enabled) if enabled is not None else None,
            time=time_value if isinstance(time_value, str) else None,
        )
        return jsonify({"enabled": bool(saved.get("enabled")), "time": str(saved.get("time", "00:00"))})

    # ---------- Webhook ----------

    def _webhook_view(cfg: dict[str, Any]) -> dict[str, Any]:
        token = str(cfg.get("token", ""))
        return {
            "enabled": bool(cfg.get("enabled")),
            "url": str(cfg.get("url", "")),
            "header_key": str(cfg.get("header_key", "")),
            "has_token": bool(token),
            "token_hint": ("••••" + token[-4:]) if token else "",
        }

    @app.get("/api/webhook")
    @require_login
    def webhook_get() -> Any:
        return jsonify(_webhook_view(web_settings.load_webhook()))

    @app.put("/api/webhook")
    @require_login
    @require_same_origin
    def webhook_put() -> Any:
        data = body_json()
        url = data.get("url")
        if url is not None and not str(url).startswith(("http://", "https://")):
            return jsonify({"error": "bad_url", "message": "Webhook 地址需以 http:// 或 https:// 开头"}), 400
        token = data.get("token")
        if token is not None and not isinstance(token, str):
            return jsonify({"error": "bad_token", "message": "token 需为字符串"}), 400
        saved = web_settings.save_webhook(
            enabled=data.get("enabled") if isinstance(data.get("enabled"), bool) else None,
            url=str(url).strip() if url is not None else None,
            header_key=str(data.get("header_key", "")).strip() if "header_key" in data else None,
            token=(token if isinstance(token, str) and token else None) if not data.get("clear_token") else "",
        )
        return jsonify(_webhook_view(saved))

    @app.post("/api/webhook/test")
    @require_login
    @require_same_origin
    def webhook_test() -> Any:
        cfg = web_settings.load_webhook()
        if not str(cfg.get("url", "")).strip():
            return jsonify({"error": "no_url", "message": "请先填写 Webhook 地址"}), 400
        sent, detail = send_test()
        if not sent:
            # detail 为接收端的真实响应（HTTP 状态 / 异常），供界面直接显示
            return jsonify({"error": "send_failed", "message": "发送失败，请检查地址与网络", "detail": detail}), 502
        return jsonify({"ok": True})

    # ---------- 报告语言 ----------

    @app.get("/api/report-lang")
    @require_login
    def report_lang_get() -> Any:
        return jsonify({"lang": web_settings.load_report_lang()})

    @app.put("/api/report-lang")
    @require_login
    @require_same_origin
    def report_lang_put() -> Any:
        data = body_json()
        lang = str(data.get("lang", "")).strip()
        if lang not in web_settings.REPORT_LANGS:
            return jsonify({"error": "bad_lang", "message": "lang 需为 zh 或 en"}), 400
        web_settings.save_report_lang(lang)
        return jsonify({"lang": lang})

    # ---------- 报告 ----------

    # 报告只读接口公开（供公开报告页 /reports/* 免登录浏览；管理数据走 /api/summary）
    @app.get("/api/reports")
    def reports() -> Any:
        return jsonify({"reports": _report_list()})

    @app.get("/api/reports/<name>")
    def report_detail(name: str) -> Any:
        # 仅允许已知命名（analysis 或 YYYY-MM-DD 每日归档），拒绝路径穿越；
        # ?lang=zh|en 选择语言：en 读 .en.md 后缀，zh 无后缀（兼容旧文件）。
        stem = Path(name).name
        if stem != FULL_REPORT_NAME and not DAILY_NAME_RE.fullmatch(stem):
            return jsonify({"error": "not_found", "message": "报告不存在"}), 404
        rlang = request.args.get("lang", "zh")
        if rlang not in ("zh", "en"):
            rlang = "zh"
        suffix = ".en" if rlang == "en" else ""
        path = REPORT_DIR / f"{stem}{suffix}.md"
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
            mtime = path.stat().st_mtime
        except OSError:
            return jsonify({"error": "not_found", "message": "报告不存在，请先运行一次分析"}), 404
        return jsonify(
            {
                "name": path.stem,
                "language": rlang,
                "updated_at": mtime,
                "content": content,
            }
        )

    @app.delete("/api/reports/<name>")
    @require_login
    @require_same_origin
    def report_delete(name: str) -> Any:
        # 删除某份报告的全部语言文件（YYYY-MM-DD.md / .en.md，或 analysis[.en].md）；
        # 仅允许已知命名，拒绝路径穿越。任务运行中拒绝，避免边写边删。
        stem = Path(name).name
        if stem != FULL_REPORT_NAME and not _valid_daily_date(stem):
            return jsonify({"error": "not_found", "message": "报告不存在"}), 404
        if tasks.running():
            return jsonify({"error": "task_running", "message": "分析任务运行中，暂不能删除报告"}), 409
        removed: list[str] = []
        for suffix in ("", ".en"):
            path = REPORT_DIR / f"{stem}{suffix}.md"
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            except OSError:
                return jsonify({"error": "io_error", "message": "删除报告失败"}), 500
            removed.append(path.name)
        if not removed:
            return jsonify({"error": "not_found", "message": "报告不存在"}), 404
        return jsonify({"ok": True, "removed": removed})

    @app.get("/api/summary")
    @require_login
    def summary() -> Any:
        return jsonify({"summary": _summary(config, secrets_store)})

    # ---------- 版本号（部署验证用：deploy.yml 末步 curl 此接口与 tag 比对） ----------

    @app.get("/api/version")
    def version() -> Any:
        try:
            v = VERSION_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            v = ""
        return jsonify({"version": v})

    # ---------- favicon（favicon.ico / favicon.svg 随前端构建产物拷贝到 static 根） ----------

    @app.get("/favicon.ico")
    def favicon_ico() -> Any:
        return send_from_directory(STATIC_DIR, "favicon.ico", mimetype="image/x-icon")

    @app.get("/favicon.svg")
    def favicon_svg() -> Any:
        return send_from_directory(STATIC_DIR, "favicon.svg", mimetype="image/svg+xml")

    # ---------- 静态页面 ----------

    @app.get("/")
    def index() -> Any:
        if not (STATIC_DIR / "index.html").exists():
            return "拾贝 Web：前端未构建，请先构建 frontend 或使用 Docker 镜像。", 503
        return send_from_directory(STATIC_DIR, "index.html")

    @app.errorhandler(404)
    def not_found(_e: Any) -> Any:
        if request.path.startswith("/api/"):
            return jsonify({"error": "not_found", "message": "接口不存在"}), 404
        return index()

    # 每日定时调度（守护线程；触发增量分析）
    scheduler = DailyScheduler(lambda: _start_task("today"))
    scheduler.start()

    return app


def main() -> None:
    port = int(os.environ.get("SHIBEI_PORT", "8000"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logger.info("拾贝 Web 启动，监听 :%s", port)
    serve(create_app(), host="0.0.0.0", port=port, threads=8)


if __name__ == "__main__":
    main()
