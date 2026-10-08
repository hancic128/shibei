#!/usr/bin/env python3
"""拾贝 · 分析模块（单一入口）：自动爬取所需数据 → 调 LLM → 输出报告。

用法：
    python3 analyzer.py          # 默认：自动增量爬取 + 增量分析（数据为空时自动全量）
    python3 analyzer.py --full   # 强制全量：自动爬取 + 重分析全部帖子
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import locale
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from crawler import run_crawl
from models import MAX_REPLIES, Post, post_from_dict

DATA_DIR = Path("data")
CACHE_DIR = DATA_DIR / ".cache"
STATE_FILE = DATA_DIR / "state.json"
REPORT_DIR = DATA_DIR / "analysis"
CONFIG_FILE = "config.json"

# 批次分析：每批帖子一次调用即输出全部类别（避免每类各发一次正文，省 2/3 输入 token）。
# 批内帖数越大，批次间重复的提示词开销越小；单次输出上限相应放大（MULTI_MAX_TOKENS）。
BATCH_SIZE = 20
MERGE_SIZE = 3  # 合并回退路径的层级合并宽度
MULTI_MAX_TOKENS = 8192  # 一次输出多类时的 max_tokens 下限
MAX_BACKOFF = 60  # 指数退避上限（秒）

LANGS = ("zh", "en")

# (key, 标题, 定义) —— 三个分析模块，互不干扰，支持中英两种分析语言。
# 对应报告页的「产品创意 / 用户痛点 / 潜在机会」三个文档，各自按分类（H3）组织。
CATEGORIES_BY_LANG: dict[str, list[tuple[str, str, str]]] = {
    "zh": [
        ("ideas", "产品创意", "帖子中提到或暗示的、有价值的想法、工具需求和产品方向"),
        ("pain", "用户痛点", "用户反复抱怨、求助、表达不满的问题"),
        ("indie", "潜在机会", "对独立开发者/小团队友好、低门槛、可快速验证的方向与机会"),
    ],
    "en": [
        (
            "ideas",
            "Product Ideas",
            "Valuable ideas, tool needs and product directions mentioned or implied in the posts",
        ),
        (
            "pain",
            "User Pain Points",
            "Problems users repeatedly complain about, seek help with or express dissatisfaction about",
        ),
        (
            "indie",
            "Opportunities",
            "Directions friendly to indie developers / small teams: low barrier, quick to validate",
        ),
    ],
}

# 兼容旧名：默认（zh）分类表，供测试与外部引用
CATEGORIES = CATEGORIES_BY_LANG["zh"]


def _categories(lang: str) -> list[tuple[str, str, str]]:
    """按语言取分类表；未知语言回落 zh。"""
    return CATEGORIES_BY_LANG.get(lang, CATEGORIES_BY_LANG["zh"])


# 评估打分：每条产品创意附 [v=价值,d=难度,✓/✗]，报告按总分（价值×(6-难度)）分桶输出。
# 配置在 config.json 的 evaluation 段；本常量定义默认值，便于回滚到旧行为。
#
# 总分阈值三层（默认）：
#   score >= keep_threshold       → 保留分桶（keep）
#   watch_threshold <= score < keep_threshold → 待观察分桶（watch）
#   score < watch_threshold       → 不输出（前置过滤，省 token）
_EVALUATION_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "keep_threshold": 16,  # 总分 ≥ 16 进「保留」（约等于"价值 4 + 难度 2"）
    "watch_threshold": 12,  # 总分 ≥ 12 但 < keep_threshold 进「待观察」
    "show_watch": True,
    "show_rejected": True,
}

# 创意条目评分字段：v=价值(1-5)、d=难度(1-5)、✓ 红线 pass / ✗ 红线 concern
# 兼容不同模型的输出风格：`[v=4,d=2,✓]`（deepseek，无空格）与 `[v=4, d=2, ✓]`（部分模型带空格）。
ITEM_SCORE_RE = re.compile(r"\[v\s*=\s*(\d)\s*,\s*d\s*=\s*(\d)\s*,\s*([✓✗])\]")


def load_evaluation(config: dict[str, Any]) -> dict[str, Any]:
    """从 config 读 evaluation 段，缺失或字段不全时用默认值兜底。"""
    out = dict(_EVALUATION_DEFAULTS)
    user_cfg = config.get("evaluation") if isinstance(config, dict) else None
    if isinstance(user_cfg, dict):
        out.update(user_cfg)
    return out


def parse_item_score(line: str) -> tuple[int, int, str, str]:
    """从条目行提取评分字段。返回 (价值, 难度, 红线符号, 去除评分字段后的原文)。

    无评分字段时返回 (0, 0, "?", 原行) — 调用方按「未评估」处理。
    """
    m = ITEM_SCORE_RE.search(line)
    if not m:
        return 0, 0, "?", line
    v, d, r = int(m.group(1)), int(m.group(2)), m.group(3)
    rest = (line[: m.start()] + line[m.end() :]).strip()
    return v, d, r, rest


def _total_score(value: int, difficulty: int) -> int:
    """总分 = 价值 × (6 - 难度)，范围 1–25。"""
    return value * (6 - difficulty)


# 创意分类 H3 标题 → 内部分桶 key 的映射（zh / en）。LLM 在 prompt 指引下会输出
# `### 保留 (...)` / `### 待观察 (...)` / `### 被否决` 等 H3；这里用宽松匹配，
# 兼容「(12)」/「(16+)」/「12-15」/全角括号「（16+）」等阈值说明的格式差异。
_EVAL_H3_PATTERNS: dict[str, list[tuple[str, re.Pattern[str]]]] = {
    "zh": [
        ("keep", re.compile(r"^###\s*(?:保留|高价值|高分)(?:\s*[（(][^）)]*[）)])?\s*$")),
        ("watch", re.compile(r"^###\s*(?:待观察|中等|中分)(?:\s*[（(][^）)]*[）)])?\s*$")),
        ("rejected", re.compile(r"^###\s*(?:被否决|红线|不通过)(?:\s*[（(][^）)]*[）)])?\s*$")),
    ],
    "en": [
        ("keep", re.compile(r"^###\s*(?:Keep|High)(?:\s*\([^)]*\))?\s*$", re.I)),
        ("watch", re.compile(r"^###\s*(?:Watch|Medium)(?:\s*\([^)]*\))?\s*$", re.I)),
        ("rejected", re.compile(r"^###\s*(?:Rejected|Red-?line)(?:\s*\([^)]*\))?\s*$", re.I)),
    ],
}


def parse_eval_h3_sections(text: str, lang: str) -> dict[str, list[str]] | None:
    """识别 LLM 直接输出的三档 H3 分桶。返回 {bucket: [行]}；若没有 H3 标记则返回 None。

    每桶里只收集 `-` 开头行（条目）。H3 之后的非 `-` 行（描述/空行）跳过。
    返回 None 时调用方应回退到基于评分字段的解析。
    """
    patterns = _EVAL_H3_PATTERNS.get(lang, _EVAL_H3_PATTERNS["zh"])
    sections: dict[str, list[str]] = {"keep": [], "watch": [], "rejected": []}
    current: str | None = None
    matched_any = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("###"):
            bucket: str | None = None
            for key, pat in patterns:
                if pat.match(line):
                    bucket = key
                    matched_any = True
                    break
            current = bucket
            continue
        if current is None:
            continue
        if not line or not line.startswith("-"):
            continue
        sections[current].append(line)
    if not matched_any:
        return None
    return sections


def categorize_items(
    text: str, eval_cfg: dict[str, Any], lang: str = "zh"
) -> dict[str, list[tuple[int, int, str, str]]]:
    """把「产品创意」section 的条目文本按红线 + 总分切成四桶，并按总分从高到低排序。

    每桶元素是 (v, d, redline, line_text)：
    - keep：红线 pass + 总分 >= keep_threshold
    - watch：红线 pass + 总分 < keep_threshold
    - rejected：红线 concern
    - unscored：无评分字段（缓存命中/旧条目/LLM 未返回评分），按原样保留

    解析策略：优先按 LLM 输出的三档 H3（`### 保留` / `### 待观察` / `### 被否决`）分桶；
    没有 H3 时回退到基于评分字段的正则解析。
    """
    keep_threshold = int(eval_cfg.get("keep_threshold", _EVALUATION_DEFAULTS["keep_threshold"]))
    buckets: dict[str, list[tuple[int, int, str, str]]] = {
        "keep": [],
        "watch": [],
        "rejected": [],
        "unscored": [],
    }

    h3 = parse_eval_h3_sections(text, lang)
    if h3 is not None:
        # 路径 A：LLM 直接按 H3 分桶（前置过滤模式）
        for bucket_key, lines in h3.items():
            for line in lines:
                # 链接还原可能让 `— [来源]` 出现在已还原文本里
                if "— [#" not in line and "— [来源]" not in line:
                    continue
                v, d, r, rest = parse_item_score(line)
                # H3 模式下分桶归属已由 H3 决定，但 score 仍按规则重新计算（防 LLM 误标）
                if r == "✗":
                    buckets["rejected"].append((v, d, r, rest))
                elif r == "?":
                    # H3 模式下出现无评分字段 → 落到 H3 指定分桶，unscored 兜底
                    buckets[bucket_key].append((0, 0, r, rest))
                else:
                    score = _total_score(v, d)
                    if bucket_key == "keep" and score < keep_threshold:
                        # LLM 误放进保留 → 移到 watch
                        buckets["watch"].append((v, d, r, rest))
                    elif bucket_key == "watch" and score >= keep_threshold:
                        # LLM 误放进待观察 → 移到 keep
                        buckets["keep"].append((v, d, r, rest))
                    else:
                        buckets[bucket_key].append((v, d, r, rest))
    else:
        # 路径 B：回退到基于评分字段的正则解析（兼容旧 prompt / 缓存）
        for raw in text.splitlines():
            line = raw.strip()
            if not line or not line.startswith("-"):
                continue
            if "— [#" not in line and "— [来源]" not in line:
                continue
            v, d, r, rest = parse_item_score(line)
            if r == "?":
                buckets["unscored"].append((0, 0, "?", rest))
            elif r == "✗":
                buckets["rejected"].append((v, d, r, rest))
            else:
                score = _total_score(v, d)
                (buckets["keep"] if score >= keep_threshold else buckets["watch"]).append((v, d, r, rest))

    def _sort_key(item: tuple[int, int, str, str]) -> int:
        v, d, r, _ = item
        return _total_score(v, d) if r != "?" else -1

    for key in buckets:
        buckets[key].sort(key=_sort_key, reverse=True)
    return buckets


def _format_eval_buckets(
    buckets: dict[str, list[tuple[int, int, str, str]]], eval_cfg: dict[str, Any], lang: str
) -> str:
    """把分桶结果格式化为报告子节（按 keep → watch → rejected → unscored 顺序）。"""
    labels = _EVAL_SECTION_LABELS.get(lang, _EVAL_SECTION_LABELS["zh"])
    sections: list[str] = []
    order = [
        ("keep", "keep_section"),
        ("watch", "watch_section"),
        ("rejected", "rejected_section"),
        ("unscored", "unscored_section"),
    ]
    for bucket_key, label_key in order:
        items = buckets.get(bucket_key, [])
        if not items:
            continue
        # rejected/unscored 默认按配置开关隐藏
        if bucket_key == "watch" and not eval_cfg.get("show_watch", True):
            continue
        if bucket_key == "rejected" and not eval_cfg.get("show_rejected", True):
            continue
        if bucket_key == "unscored" and not eval_cfg.get("show_watch", True):
            # 未评分条目与待观察共用 show_watch 开关：避免默认输出过多
            continue
        heading = labels[label_key].format(n=len(items))
        sections.append(f"### {heading}\n")
        for _v, _d, _r, rest in items:
            sections.append(f"{rest}\n")
        sections.append("")
    return "\n".join(sections).rstrip()


_EVAL_SECTION_LABELS: dict[str, dict[str, str]] = {
    "zh": {
        "summary_line": "本轮 {total} 条创意：保留 {keep} / 待观察 {watch} / 被否决 {rejected} / 未评分 {unscored}",
        "criteria_line": "评估阈值：总分 ≥ {threshold}；总分 = 价值 × (6 - 难度)；红线命中（✗）即否决。",
        "keep_section": "保留清单 ({n})",
        "watch_section": "待观察 ({n})",
        "rejected_section": "被否决 ({n})",
        "unscored_section": "未评分 ({n})",
    },
    "en": {
        "summary_line": (
            "This round {total} ideas: keep {keep} / watch {watch} / rejected {rejected} / unscored {unscored}"
        ),
        "criteria_line": "Threshold: total ≥ {threshold}; total = value × (6 - difficulty); red-line hit (✗) rejects.",
        "keep_section": "Keep ({n})",
        "watch_section": "Watch ({n})",
        "rejected_section": "Rejected ({n})",
        "unscored_section": "Unscored ({n})",
    },
}


def _format_eval_summary(
    buckets: dict[str, list[tuple[int, int, str, str]]], eval_cfg: dict[str, Any], lang: str
) -> str:
    """评估汇总行（引用块形式）+ 阈值说明。"""
    labels = _EVAL_SECTION_LABELS.get(lang, _EVAL_SECTION_LABELS["zh"])
    summary = labels["summary_line"].format(
        total=sum(len(v) for v in buckets.values()),
        keep=len(buckets["keep"]),
        watch=len(buckets["watch"]),
        rejected=len(buckets["rejected"]),
        unscored=len(buckets["unscored"]),
    )
    criteria = labels["criteria_line"].format(threshold=eval_cfg.get("keep_threshold", 16))
    return f"> {summary}\n> {criteria}"


_EVAL_CATEGORY_KEY = "ideas"  # 评估打分仅作用于「产品创意 / Product Ideas」分类


# 来源链接锚点：最终输出时由代码还原为可点击链接（不经过 LLM）
LINK_RE = re.compile(r"\[#([^\]]+)\]")

# LLM 配置（main 里从 config/环境变量解析后填充），call_api 读取
_LLM: dict[str, str] = {"base_url": "", "model": "", "max_tokens": "4096"}

# 分析/界面语言（main 里从 --lang / ANALYZE_LANG / 系统语言解析后填充），默认中文
_LANG: str = "zh"


def system_lang() -> str:
    """系统语言：LANG / LC_ALL / LC_MESSAGES 或 locale 设置；en* → en，其余 → zh。"""
    candidates = [os.environ.get(key, "") for key in ("LC_ALL", "LC_MESSAGES", "LANG")]
    try:
        candidates.append(locale.getlocale()[0] or "")
    except (ValueError, TypeError):
        pass
    for candidate in candidates:
        tag = str(candidate).strip().lower()
        if not tag or tag in ("c", "posix"):
            continue
        if tag.startswith("en"):
            return "en"
        if tag.startswith("zh"):
            return "zh"
    return "zh"


def resolve_lang(lang: str | None) -> str:
    """解析分析语言：--lang 参数 > ANALYZE_LANG 环境变量 > 系统语言 > zh。"""
    for candidate in (lang, os.environ.get("ANALYZE_LANG", "")):
        if candidate is not None and str(candidate).strip() in LANGS:
            return str(candidate).strip()
    return system_lang()


# CLI / 界面文案（按分析语言输出，让任务日志随语言切换）
_UI: dict[str, dict[str, str]] = {
    "zh": {
        "no_api_key": (
            "未设置 OPENAI_API_KEY。\n"
            "拾贝使用 OpenAI 兼容 API，请自行提供 base_url、api_key 与模型名：\n"
            "  export OPENAI_API_KEY=sk-xxx   # 必填\n"
            "  export OPENAI_BASE_URL=https://api.deepseek.com/v1   # 必填（或用 config.json 的 llm.base_url）\n"
            "  export ANALYZE_MODEL=deepseek-v4-flash   # 必填（或用 config.json 的 llm.model）"
        ),
        "no_base_url": (
            "未设置 OPENAI_BASE_URL。\n"
            "拾贝需要用户自带的 OpenAI 兼容 API 地址：\n"
            "  export OPENAI_BASE_URL=https://api.deepseek.com/v1\n"
            "  或写入 config.json 的 llm.base_url"
        ),
        "no_model": (
            "未设置 ANALYZE_MODEL。\n"
            "拾贝需要指定模型名（不同厂商支持的模型各不相同）：\n"
            "  export ANALYZE_MODEL=deepseek-v4-flash\n"
            "  或写入 config.json 的 llm.model"
        ),
        "link_warn": "[!] 链接还原：未找到帖子 {pid} 的来源映射，保留原文",
        "report_written": "\n报告已写入：{path}",
        "interrupted": "\n[!] 已中断（Ctrl+C）。已爬取/分析的数据均已保存，下次运行自动继续。",
        "first_run_full": "首次运行或数据为空，自动全量爬取 ...",
        "posts_to_analyze": "共 {n} 个帖子待分析（来源: {summary}）...",
        "full_title": "{day} 全量总览",
        "no_new_latest": "没有新增帖子，无需分析。最近一次报告：{path}",
        "no_new_plain": "没有新增帖子，无需分析。",
        "no_posts": "没有可分析的帖子。自动爬取未获取到数据——",
        "no_posts_hint": "可能是来源 API 限流或暂时不可用（如 V2EX 403），请稍后重试或检查网络。",
    },
    "en": {
        "no_api_key": (
            "OPENAI_API_KEY is not set.\n"
            "Shibei uses an OpenAI-compatible API — provide base_url, api_key and model yourself:\n"
            "  export OPENAI_API_KEY=sk-xxx   # required\n"
            "  export OPENAI_BASE_URL=https://api.deepseek.com/v1   # required (or llm.base_url in config.json)\n"
            "  export ANALYZE_MODEL=deepseek-v4-flash   # required (or llm.model in config.json)"
        ),
        "no_base_url": (
            "OPENAI_BASE_URL is not set.\n"
            "Shibei needs your own OpenAI-compatible API endpoint:\n"
            "  export OPENAI_BASE_URL=https://api.deepseek.com/v1\n"
            "  or set llm.base_url in config.json"
        ),
        "no_model": (
            "ANALYZE_MODEL is not set.\n"
            "Shibei needs a model name (models vary by provider):\n"
            "  export ANALYZE_MODEL=deepseek-v4-flash\n"
            "  or set llm.model in config.json"
        ),
        "link_warn": "[!] Restore links: no source mapping for post {pid}, keeping original text",
        "report_written": "\nReport written to: {path}",
        "interrupted": "\n[!] Interrupted (Ctrl+C). Crawled/analyzed data is saved; the next run will continue.",
        "first_run_full": "First run or no data — running full crawl ...",
        "posts_to_analyze": "{n} posts to analyze (sources: {summary})...",
        "full_title": "{day} · Full Overview",
        "no_new_latest": "No new posts, nothing to analyze. Latest report: {path}",
        "no_new_plain": "No new posts, nothing to analyze.",
        "no_posts": "No posts to analyze — the auto crawl returned no data.",
        "no_posts_hint": (
            "The source API may be rate-limited or temporarily unavailable "
            "(e.g. V2EX 403). Retry later or check the network."
        ),
    },
}


def _t(lang: str, key: str, **kw: Any) -> str:
    """按语言取 CLI/界面文案；未知语言回落 zh。"""
    table = _UI.get(lang) or _UI["zh"]
    return table.get(key, "").format(**kw)


# ---------- 配置 / 状态 ----------


def load_config(path: str = CONFIG_FILE) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_state(path: Path | None = None) -> dict[str, dict[str, int]]:
    path = path or STATE_FILE
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_state(state: dict[str, dict[str, int]], path: Path | None = None) -> None:
    path = path or STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def load_json(path: Path) -> Any:
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def resolve_llm_config(config: dict[str, Any], lang: str = "zh") -> dict[str, str]:
    """解析 LLM 配置。

    URL 与模型均必填（环境变量或 config.json 二选一），缺失即退出——
    避免误发到错误的端点或模型。max_tokens 可选，兜底 4096。
    """
    base_url = os.environ.get("OPENAI_BASE_URL") or config.get("llm", {}).get("base_url")
    if not base_url:
        raise SystemExit(_t(lang, "no_base_url"))
    model = os.environ.get("ANALYZE_MODEL") or config.get("llm", {}).get("model")
    if not model:
        raise SystemExit(_t(lang, "no_model"))
    max_tokens = str(os.environ.get("ANALYZE_MAX_TOKENS") or config.get("llm", {}).get("max_tokens") or "4096")
    return {"base_url": base_url.rstrip("/"), "model": model, "max_tokens": max_tokens}


def check_env() -> str:
    """检查 OPENAI_API_KEY，缺失则打印配置说明并退出。"""
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        print(_t(_LANG, "no_api_key"), file=sys.stderr)
        raise SystemExit(1)
    return api_key


# ---------- API 调用 ----------


def _http_error_detail(e: urllib.error.HTTPError) -> str:
    """从 OpenAI 兼容 API 的错误响应中提取可读的 message。"""
    try:
        data = json.loads(e.read().decode("utf-8"))
    except (ValueError, OSError):
        return str(e)
    err = data.get("error", {})
    if isinstance(err, dict):
        return str(err.get("message") or err)
    return str(err)


def call_api(prompt: str, *, timeout: int = 120, retries: int = 3, max_tokens: int | None = None) -> str:
    """调用 OpenAI 协议 chat/completions，返回 assistant 文本。失败时 raise（附服务端原因）。

    max_tokens 缺省用配置值；一次输出多类的调用需要更大的上限时显式传入。

    Token 消耗：尝试从服务端响应里读 `usage.prompt_tokens` / `usage.completion_tokens`，
    累加到模块级 `_TOKEN_USAGE` 字典（按 prompt / completion / total 拆分）。
    服务端不返回 usage（旧协议 / 网关剥掉）时不报错，仅不计入；打印的 token 摘要会标注。
    """
    body = json.dumps(
        {
            "model": _LLM["model"],
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "max_tokens": max_tokens or int(_LLM.get("max_tokens", "4096")),
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{_LLM['base_url']}/chat/completions",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {os.environ.get('OPENAI_API_KEY', '')}",
            "Content-Type": "application/json",
        },
    )
    last_error = ""
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                data = json.loads(raw)
                # 服务端可能不返回 usage（部分网关、自定义代理）；失败/缺失就跳过
                usage = data.get("usage") if isinstance(data, dict) else None
                if isinstance(usage, dict):
                    p = int(usage.get("prompt_tokens") or 0)
                    c = int(usage.get("completion_tokens") or 0)
                    _TOKEN_USAGE["prompt"] += p
                    _TOKEN_USAGE["completion"] += c
                    _TOKEN_USAGE["total"] += p + c
                    _TOKEN_USAGE["calls"] += 1
                return data["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            detail = _http_error_detail(e)
            if 400 <= e.code < 500 and e.code != 429:
                # 客户端错误（模型名/参数/鉴权），重试无用，立即终止并给出原因
                raise RuntimeError(f"LLM API 返回 {e.code}：{detail}") from e
            last_error = f"HTTP {e.code}：{detail}"
        except (urllib.error.URLError, OSError, ValueError) as e:
            last_error = str(e)
        if attempt < retries:
            time.sleep(min(5 * (2**attempt), MAX_BACKOFF))
    raise RuntimeError(f"LLM API 调用失败（重试 {retries} 次后）：{last_error}")


# 模块级 token 累计；call_api 写入，print_token_usage 打印并清零。
_TOKEN_USAGE: dict[str, int] = {"prompt": 0, "completion": 0, "total": 0, "calls": 0}


def reset_token_usage() -> None:
    """重置 token 累计（单测/手动控制边界）。"""
    _TOKEN_USAGE["prompt"] = 0
    _TOKEN_USAGE["completion"] = 0
    _TOKEN_USAGE["total"] = 0
    _TOKEN_USAGE["calls"] = 0


def token_usage_snapshot() -> dict[str, int]:
    """返回当前累计的 token 用量快照（不影响累计）。"""
    return dict(_TOKEN_USAGE)


def format_token_usage() -> str:
    """格式化 token 用量摘要：prompt + completion + total + 调用次数。

    若累计 calls==0（服务端未返回 usage），摘要里标注"（服务端未返回 usage）"。
    """
    snap = _TOKEN_USAGE
    if snap["calls"] == 0:
        return f"token: 服务端未返回 usage（{snap['calls']} 次调用）"
    return (
        f"token: prompt={snap['prompt']} + completion={snap['completion']} "
        f"= total={snap['total']}（{snap['calls']} 次调用）"
    )


# ---------- Prompt ----------


def format_post(p: Post) -> str:
    """帖子文本格式：正文截断 500 字符，每帖最多 10 条回复，每条回复截断 200 字符。"""
    lines = [
        f"## [#{p.id}] {p.title}",
        f"来源: {p.source} | 节点: {p.node} | 作者: {p.author} | 回复数: {p.replies_count}",
    ]
    if p.content:
        lines.append(p.content[:500])
    for r in p.reply_list[:MAX_REPLIES]:
        if r.content:
            lines.append(f"  - {r.author}: {r.content[:200]}")
    return "\n".join(lines)


def format_batch(batch: list[Post]) -> str:
    return "\n---\n".join(format_post(p) for p in batch)


def _eval_rule_zh(keep_threshold: int, watch_threshold: int) -> str:
    """中文评估打分 + 前置过滤规则段（注入 prompt）。

    LLM 在生成时即按总分（v × (6-d)）分桶：
    - ≥ keep：输出在 ### 保留
    - watch..keep-1：输出在 ### 待观察
    - < watch：直接不输出（前置过滤，省 token）
    - 红线 ✗：输出在 ### 被否决
    """
    return (
        f"- 先给每条打分 `[v=X,d=Y,✓/✗]`：v 价值（1–5）、d 难度（1–5）、"
        f"✓ 红线 pass / ✗ 命中法律·道德·合规·技术不可控红线\n"
        f"- 评分必须给具体数字（如 `[v=4,d=2,✓]`），不得用文字代替；评分字段位置在描述末尾、`[#帖子ID]` 之前\n"
        f"- 按总分 v×(6-d) 分桶输出（每组用三级标题分组，不要总标题外的其他标题）：\n"
        f"  · 总分 ≥ {keep_threshold} 的输出在 `### 保留 ({keep_threshold}+)`\n"
        f"  · 总分在 [{watch_threshold}, {keep_threshold}) 内的输出在 `### 待观察`\n"
        f"  · 红线 ✗ 的输出在 `### 被否决`\n"
        f"  · 总分 < {watch_threshold} 的直接不输出（前置过滤，省 token）\n"
        f"- 组内条目按总分从高到低排列"
    )


def _eval_rule_en(keep_threshold: int, watch_threshold: int) -> str:
    """英文评估打分 + 前置过滤规则段（注入 prompt）。"""
    return (
        f"- Score each item `[v=X,d=Y,✓/✗]`: v=value (1–5), d=difficulty (1–5), "
        f"✓ passes red-line / ✗ hits law·ethics·compliance·technical-uncontrollable\n"
        f"- Use literal digits (e.g. `[v=4,d=2,✓]`); place the tag before ` — [#postID]`\n"
        f"- Bucket by total = v×(6-d) (use `### ` H3 headings; no other headings):\n"
        f"  · Total ≥ {keep_threshold} → `### Keep ({keep_threshold}+)`\n"
        f"  · Total ≥ {watch_threshold} and < {keep_threshold} → `### Watch ({watch_threshold}-{keep_threshold - 1})`\n"
        f"  · Red-line ✗ → `### Rejected`\n"
        f"  · Total < {watch_threshold} → omit entirely (front-end filter, save tokens)\n"
        f"- Within each bucket, sort by total descending"
    )


def build_batch_prompt(
    batch_text: str,
    idx: int,
    total: int,
    title: str,
    desc: str,
    lang: str = "zh",
    eval_enabled: bool = False,
    eval_cfg: dict[str, Any] | None = None,
) -> str:
    """单类批次 prompt；类别标题/说明由调用方按分析语言传入。

    eval_enabled=True 时在末尾追加评估打分 + 前置过滤指令（仅「产品创意 / Product Ideas」分类启用）。
    eval_cfg 缺省时用 _EVALUATION_DEFAULTS 的阈值（keep=16, watch=12）。
    """
    cfg = eval_cfg if isinstance(eval_cfg, dict) else _EVALUATION_DEFAULTS
    keep_threshold = int(cfg.get("keep_threshold", _EVALUATION_DEFAULTS["keep_threshold"]))
    watch_threshold = int(cfg.get("watch_threshold", _EVALUATION_DEFAULTS["watch_threshold"]))
    if lang == "en":
        rules = (
            "Rules:\n"
            "- Output a plain item list, no overall heading\n"
            "- Output only this category, nothing else\n"
            '- If nothing matches the definition, output "None"\n'
            "- Do not limit the count; extract as much valuable info as possible\n"
            "- Mark each item's source post with ` — [#postID]` at the end; IDs must exactly match post labels above\n"
            "- Answer in English; even if the original text is in Chinese, output in English"
        )
        if eval_enabled:
            rules += "\n" + _eval_rule_en(keep_threshold, watch_threshold)
        return f"""Analyze the community posts below; extract only "{title}" items.

Definition: {desc}

{rules}

---(batch {idx + 1}/{total})

{batch_text}"""
    rules = (
        "规则：\n"
        "- 直接输出条目列表，不加总标题\n"
        "- 只输出这一类，不要输出其他类别\n"
        "- 如果没有符合定义的信息，输出「无」\n"
        "- 不要限制条数，尽可能多地提炼有价值的信息\n"
        "- 每条在描述末尾用 ` — [#帖子ID]` 标注来源帖子，ID 必须与上文的帖子标注完全一致，不得改写\n"
        "- 一律用中文回答；即使原文是英文，也要用中文输出"
    )
    if eval_enabled:
        rules += "\n" + _eval_rule_zh(keep_threshold, watch_threshold)
    return f"""分析以下社区帖子，只提炼「{title}」类信息。

定义：{desc}

{rules}

---（第 {idx + 1}/{total} 批）

{batch_text}"""


def build_multi_prompt(
    batch_text: str,
    idx: int,
    total: int,
    lang: str = "zh",
    eval_enabled: bool = False,
    eval_cfg: dict[str, Any] | None = None,
) -> str:
    """一次调用提炼全部类别：帖子正文只发一次，避免按类重复输入（省 token）。

    eval_enabled=True 时，仅在「产品创意 / Product Ideas」分类的 section 追加评估打分 + 前置过滤规则；
    「用户痛点」「潜在机会」不受影响（不在 idea-eval 范围）。
    eval_cfg 缺省时用 _EVALUATION_DEFAULTS 的阈值。
    """
    cfg = eval_cfg if isinstance(eval_cfg, dict) else _EVALUATION_DEFAULTS
    keep_threshold = int(cfg.get("keep_threshold", _EVALUATION_DEFAULTS["keep_threshold"]))
    watch_threshold = int(cfg.get("watch_threshold", _EVALUATION_DEFAULTS["watch_threshold"]))
    eval_rule = (
        _eval_rule_en(keep_threshold, watch_threshold)
        if lang == "en"
        else _eval_rule_zh(keep_threshold, watch_threshold)
    )

    def _section_lines(key: str, title: str, desc: str) -> str:
        if lang == "en":
            base = f"## {title}\n(Definition: {desc})"
        else:
            base = f"## {title}\n（定义：{desc}）"
        if eval_enabled and key == _EVAL_CATEGORY_KEY:
            if lang == "en":
                base += "\n(Score rule: " + eval_rule + ")"
            else:
                base += "\n（评估规则：" + eval_rule + "）"
        return base

    sections = "\n".join(_section_lines(key, title, desc) for key, title, desc in _categories(lang))
    if lang == "en":
        return f"""Analyze the posts below; extract info per listed category (send the batch once, output in one go).

{sections}

Rules:
- In order above, start each category with `## Category name` (exact match), then its items; write "None" if empty
- No overall heading, no extra explanations, no unlisted categories
- Keep descriptions concise: one sentence per item, no more than 60 words
- Mark each item's source post with ` — [#postID]` at the end; IDs must exactly match the post labels above
- Answer in English; even if the original text is in Chinese, output in English

---(batch {idx + 1}/{total})

{batch_text}"""
    return f"""分析以下社区帖子，按下面每个类别分别提炼信息（同一批帖子只发一次，请一次全部输出）。

{sections}

规则：
- 按上面的顺序，每个类别先写 `## 类别名`（必须与上面完全一致），再列出该类条目；无匹配信息时写「无」
- 不要输出总标题、不要额外解释、不要输出未列出的类别
- 描述务必精简：每条一句话、不超过 60 字，去掉客套与重复限定
- 每条在描述末尾用 ` — [#帖子ID]` 标注来源帖子，ID 必须与上文的帖子标注完全一致，不得改写
- 一律用中文回答；即使原文是英文，也要用中文输出

---（第 {idx + 1}/{total} 批）

{batch_text}"""


_MULTI_HEAD_RE = re.compile(r"^##\s*(.+?)\s*$", re.M)


def parse_multi(raw: str, lang: str = "zh") -> dict[str, str]:
    """把一次调用的多类输出解析为 {key: 该类文本}；缺失的类别返回空串。"""
    cats = _categories(lang)
    out: dict[str, str] = {key: "" for key, _, _ in cats}
    if not raw:
        return out
    buckets: dict[str, list[str]] = {}
    current = ""
    for line in raw.split("\n"):
        m = _MULTI_HEAD_RE.match(line.strip())
        if m:
            current = m.group(1).strip()
            buckets.setdefault(current, [])
            continue
        if current:
            buckets[current].append(line)
    for key, title, _ in cats:
        text = "\n".join(buckets.get(title, [])).strip()
        out[key] = "" if is_empty_result(text, lang) else text
    return out


def build_merge_prompt(results: list[str], incremental: bool, lang: str = "zh") -> str:
    """合并去重 prompt：把多批结果合并成条目列表。

    空输入守卫：上层 consolidate/merge_results 已先过滤，但任何绕过入口直接调用本函数
    的代码都会发出空 body prompt 给 LLM（LLM 仍可能回 "未提供可合并的多批次分析结果…"），回
    复会穿透到报告层。直接拒绝空列表，让上层走 build_report 的 empty_text 分支。

    评分字段保留：若条目带 `[v=X,d=Y,✓/✗]` 评分字段（仅「产品创意」分类会出现），
    合并去重时必须原样保留，不得删除或改写分数与符号。
    """
    if not results:
        raise ValueError("build_merge_prompt 需要至少 1 个非空批次结果")
    if lang == "en":
        note = " (this is today's incremental analysis)" if incremental else ""
        body = "\n\n---\n\n".join(results)
        return f"""Below are analysis results from multiple batches; merge and dedupe them into an item list{note}.

Rules:
- Output a plain item list, no overall heading
- Drop duplicate items, keep the most representative description
- Do not limit the count; keep all valuable info
- Keep the trailing ` — [#postID]` source marker on every item; do not remove or alter it
- If an item carries a score tag `[v=X,d=Y,✓/✗]`, keep the tag verbatim (digits and symbol); do not edit or drop it
- Sort by value from high to low
- Answer in English; even if the original text is in Chinese, output in English

{body}"""
    note = "（本次为今日新增分析）" if incremental else ""
    body = "\n\n---\n\n".join(results)
    return f"""以下是多批次的分析结果，请合并去重，输出条目列表{note}。

规则：
- 直接输出条目列表，不加总标题
- 去除重复条目，保留最有代表性的描述
- 不要限制条数，尽可能保留所有有价值的信息
- 每条保留描述末尾的 ` — [#帖子ID]` 来源标注，不得删除或改写
- 若条目带有评分字段 `[v=X,d=Y,✓/✗]`（仅「产品创意」分类），必须原样保留数字与符号，不得删除或改写
- 按价值从高到低排列
- 一律用中文回答；即使原文是英文，也要用中文输出

{body}"""


def build_organize_prompt(text: str, category_title: str, lang: str = "zh") -> str:
    """分组整理 prompt：只做「子主题分组」，不增删改条目。

    评分字段保留：若条目带 `[v=X,d=Y,✓/✗]` 评分字段（仅「产品创意」分类），
    分组整理时必须原样保留，不得删除或改写。
    """
    if lang == "en":
        return f"""Below is the item list for "{category_title}". Group it only; do not change any item.

Rules:
- Do not add, remove or change the meaning of any item; keep the trailing ` — [#postID]` marker on each item
- If an item carries a score tag `[v=X,d=Y,✓/✗]`, keep the tag verbatim; do not edit or drop it
- Group similar items: start each group with `### Subtopic` (<=10 words), then list its items
- At most 8 groups; if items are too scattered to group sensibly, output the whole list as-is (no ### lines)
- Sort groups (and items within groups) by value from high to low
- Answer in English; even if the original text is in Chinese, output in English

---Content to organize---

{text}"""
    return f"""以下是「{category_title}」类的分析条目列表。请只做整理分组，不要增删或改写任何条目。

规则：
- 不新增、不删除、不改写任何条目的含义；每条保留末尾的 ` — [#帖子ID]` 来源标注
- 若条目带有评分字段 `[v=X,d=Y,✓/✗]`（仅「产品创意」分类），必须原样保留数字与符号，不得删除或改写
- 把主题相近的条目归为一组：每组先用一行 `### 子主题名` 开头（子主题名不超过 10 字），随后列出该组全部条目
- 组数最多 8 组；条目过于零散、无法合理分组时，原样输出整个列表（不要添加任何 ### 行）
- 组间按价值从高到低排列
- 一律用中文回答；即使原文是英文，也要用中文输出

---待整理内容---

{text}"""


def organize_topics(text: str, category_title: str, lang: str = "zh") -> str:
    """把合并后的单个分类文本按子主题分组（输出 H3 小节），失败时原样回退。

    分组只是展示优化：任何异常（LLM 失败/空返回）都不影响报告内容本身。
    """
    if not text or not text.strip():
        return text
    try:
        grouped = call_api(build_organize_prompt(text, category_title, lang), timeout=180)
    except Exception:
        return text
    if not grouped or not grouped.strip():
        return text
    return grouped


def build_consolidate_prompt(results: list[str], category_title: str, incremental: bool, lang: str = "zh") -> str:
    """一次完成「合并去重 + 分类分组」，省掉「先全量合并、再全量分组」的第二轮调用。

    空输入守卫：见 build_merge_prompt。consolidate 入口已过滤，但任何外部直接调用必须先
    经过这里。

    评分字段保留：若条目带 `[v=X,d=Y,✓/✗]` 评分字段（仅「产品创意」分类），
    合并分组时必须原样保留，不得删除或改写。
    """
    if not results:
        raise ValueError("build_consolidate_prompt 需要至少 1 个非空批次结果")
    if lang == "en":
        note = " (this is today's incremental analysis)" if incremental else ""
        body = "\n\n---\n\n".join(results)
        return f"""Below are "{category_title}" items from multiple batches{note}. Merge, dedupe, group in one pass.

Rules:
- Do not add or rewrite items; merge duplicates, keep the most representative one
- Output structure: start each group with `### Group name` (<=10 words), then its items; at most 8 groups
- If items are too few or too scattered to group, output the plain item list (no ### lines)
- Sort groups and in-group items by value from high to low
- Keep the trailing ` — [#postID]` marker; do not remove or alter it
- If an item carries a score tag `[v=X,d=Y,✓/✗]`, keep the tag verbatim; do not edit or drop it
- Keep descriptions concise: one sentence per item, no more than 60 words
- Answer in English; even if the original text is in Chinese, output in English

---Content to organize---

{body}"""
    note = "（本次为今日新增分析）" if incremental else ""
    body = "\n\n---\n\n".join(results)
    return f"""以下是多批次分析得到的「{category_title}」条目{note}。请合并去重并分组整理，一次输出。

规则：
- 不新增、不改写条目含义；合并重复条目，保留最有代表性的一条
- 输出结构：每组先写一行 `### 分类名`（不超过 10 字），随后列出该组条目；组数最多 8 组
- 条目过少或无法合理分组时，直接输出条目列表（不要添加任何 ### 行）
- 每组与组内条目均按价值从高到低排列
- 每条保留描述末尾的 ` — [#帖子ID]` 来源标注，不得删除或改写
- 若条目带有评分字段 `[v=X,d=Y,✓/✗]`（仅「产品创意」分类），必须原样保留数字与符号，不得删除或改写
- 描述务必精简：每条一句话、不超过 60 字
- 一律用中文回答；即使原文是英文，也要用中文输出

---待整理内容---

{body}"""


def consolidate(results: list[str], category_title: str, incremental: bool, lang: str = "zh") -> str:
    """合并去重 + 分类分组。

    单批结果交给分组整理（organize_topics）；多批结果一次调用完成两步。
    调用失败或空返回时回退到「层级合并 + 分组」两步路径，保证不丢内容。
    """
    results = [r for r in results if not is_empty_result(r, lang)]
    if not results:
        return ""
    if len(results) == 1:
        return organize_topics(results[0], category_title, lang)
    try:
        merged = call_api(build_consolidate_prompt(results, category_title, incremental, lang), timeout=600)
        if merged and merged.strip():
            return merged
    except Exception:
        pass
    try:
        return organize_topics(merge_results(results, incremental, lang), category_title, lang)
    except Exception:
        # 两级都失败：原样拼接各批结果，保证不丢内容
        return "\n\n".join(results)


# ---------- 合并与链接还原 ----------


_EMPTY_RESULT: dict[str, set[str]] = {
    "zh": {"无", "没有"},
    "en": {"none", "nothing", "n/a", "nil", "no"},
}


def is_empty_result(text: str, lang: str = "zh") -> bool:
    """批次分析结果是否为空（LLM 按指令输出「无/None」或没输出内容）。"""
    t = text.strip().rstrip("。.!！")
    if not t:
        return True
    keywords = _EMPTY_RESULT.get(lang, _EMPTY_RESULT["zh"])
    return t.lower() in keywords


def merge_results(results: list[str], incremental: bool, lang: str = "zh") -> str:
    """层级合并：每 MERGE_SIZE 个一组，递归直到只剩 1 个结果。"""
    results = [r for r in results if not is_empty_result(r, lang)]
    if not results:
        return ""
    if len(results) == 1:
        return results[0]
    current = list(results)
    while len(current) > 1:
        chunks = [current[i : i + MERGE_SIZE] for i in range(0, len(current), MERGE_SIZE)]
        nxt: list[str] = []
        for chunk in chunks:
            if len(chunk) == 1:
                nxt.append(chunk[0])
            else:
                nxt.append(call_api(build_merge_prompt(chunk, incremental, lang), timeout=300))
        current = nxt
    return current[0]


def restore_links(text: str, id2link: dict[str, tuple[str, str]], lang: str = "zh") -> str:
    """把 `[#帖子ID]` 还原为 `[来源](原帖URL)`（代码层，URL 不经过 LLM）。

    只保留可点击的来源链接，不显示帖子标题。
    """

    def _repl(m: re.Match[str]) -> str:
        pid = m.group(1)
        info = id2link.get(pid)
        if info is None:
            print(_t(lang, "link_warn", pid=pid), file=sys.stderr)
            return m.group(0)
        _title, url = info
        return f"[来源]({url})"

    return LINK_RE.sub(_repl, text)


def cleanup_cache(run_id: str) -> None:
    for f in CACHE_DIR.glob(f"{run_id}_*.json"):
        f.unlink(missing_ok=True)


# ---------- 分析主流程 ----------


def analyze(
    topics: list[Post],
    incremental: bool = False,
    language: str = "zh",
    eval_enabled: bool = False,
    eval_cfg: dict[str, Any] | None = None,
    print_tokens: bool = True,
) -> dict[str, str]:
    """分析所有帖子，返回 {类别标题（按语言）: 分组整理后的文本}。

    token 优化：每批帖子只调用一次（一次输出全部类别，正文不再按类重复发送），
    每类再各调用一次完成「合并去重 + 分类分组」。

    eval_enabled=True 时，多类 prompt 的「产品创意 / Product Ideas」分类会追加评估打分 + 三档分桶指令
    （LLM 在生成时按总分阈值前置过滤，省 token）；单类回退路径下，仅对 ideas 分类追加；
    其余分类不受影响。
    eval_cfg 缺省时用 _EVALUATION_DEFAULTS 的阈值；只对「产品创意」分类生效。
    print_tokens=False 时不打印 token 摘要（单测用）。
    """
    cats = _categories(language)
    run_id = hashlib.md5("".join(p.id for p in topics).encode("utf-8")).hexdigest()[:12]
    id2link = {p.id: (p.title, p.url) for p in topics}
    batches = [topics[i : i + BATCH_SIZE] for i in range(0, len(topics), BATCH_SIZE)]
    total_batches = len(batches)
    eff_eval_cfg = eval_cfg if isinstance(eval_cfg, dict) else _EVALUATION_DEFAULTS

    def analyze_batch(bi: int, batch_text: str) -> dict[str, str]:
        """单批一次调用输出全部类别，带 run_id 缓存；输出未按类别分节时回退为按类逐次调用。

        缓存文件名带语言后缀：同一批帖子多语言运行时互不误命中。
        """
        cache_file = CACHE_DIR / f"{run_id}_b{bi}_multi_{language}.json"
        cached = load_json(cache_file)
        if isinstance(cached, dict) and isinstance(cached.get("result"), dict):
            return cached["result"]
        parsed = parse_multi(
            call_api(
                build_multi_prompt(
                    batch_text,
                    bi,
                    total_batches,
                    language,
                    eval_enabled=eval_enabled,
                    eval_cfg=eff_eval_cfg,
                ),
                max_tokens=MULTI_MAX_TOKENS,
            ),
            language,
        )
        if not any(parsed.values()):
            parsed = {
                key: call_api(
                    build_batch_prompt(
                        batch_text,
                        bi,
                        total_batches,
                        title,
                        desc,
                        language,
                        eval_enabled=(eval_enabled and key == _EVAL_CATEGORY_KEY),
                        eval_cfg=eff_eval_cfg,
                    )
                )
                for key, title, desc in cats
            }
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump({"result": parsed}, f, ensure_ascii=False)
        return parsed

    per_key: dict[str, list[str]] = {key: [] for key, _, _ in cats}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        # 批次并行：每批一次调用输出全部类别
        batch_results = [
            fut.result() for fut in (ex.submit(analyze_batch, bi, format_batch(b)) for bi, b in enumerate(batches))
        ]
        for parsed in batch_results:
            for key, text in parsed.items():
                if text:
                    per_key[key].append(text)

        # 每类一次调用：合并去重 + 分类分组（失败自动回退，见 consolidate）
        grouped_raw = {
            key: fut.result()
            for fut, key in (
                (ex.submit(consolidate, per_key[key], title, incremental, language), key) for key, title, _ in cats
            )
        }

    result: dict[str, str] = {}
    for key, title, _ in cats:
        result[title] = restore_links(grouped_raw[key], id2link, language)

    cleanup_cache(run_id)
    if print_tokens:
        print(format_token_usage())
    return result


# ---------- 帖子加载 / 报告 ----------


def load_topics(data_dir: Path, since: int | None = None) -> list[Post]:
    """遍历目录下所有 *.json，按文件名排序；since 过滤 created < since 的帖子。"""
    if not data_dir.is_dir():
        return []
    posts: list[Post] = []
    for f in sorted(data_dir.glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        p = post_from_dict(d)
        if since is not None and p.created < since:
            continue
        posts.append(p)
    return posts


def build_report(
    merged: dict[str, str],
    total: int,
    summary: str,
    title: str,
    lang: str = "zh",
    eval_cfg: dict[str, Any] | None = None,
) -> str:
    """按语言拼装报告：标题 + 来源摘要 + 帖子数 + 各分类小节。

    eval_cfg=None 或 eval_cfg["enabled"]=False 时退化为旧行为（所有分类原样输出）。
    评估启用时，「产品创意 / Product Ideas」分类按总分（价值 × (6-难度)）分桶：
    保留清单 → 待观察 → 被否决 → 未评分；其余分类原样输出。
    """
    if lang == "en":
        header = ["Sources: " + summary, "", f"Generated from {total} posts", ""]
        empty_text = "No insights found this round"
    else:
        header = [f"来源: {summary}", "", f"基于 {total} 个帖子自动生成", ""]
        empty_text = "本轮未发现相关信息"
    lines = [f"# {title}", ""] + header

    eval_title = None
    eval_cfg_eff: dict[str, Any] | None = None
    if eval_cfg and eval_cfg.get("enabled", True):
        eval_cfg_eff = eval_cfg
        for key, t, _ in _categories(lang):
            if key == _EVAL_CATEGORY_KEY:
                eval_title = t
                break

    for t, text in merged.items():
        if eval_title and t == eval_title and text.strip() and eval_cfg_eff is not None:
            buckets = categorize_items(text, eval_cfg_eff, lang)
            summary_text = _format_eval_summary(buckets, eval_cfg_eff, lang)
            bucket_text = _format_eval_buckets(buckets, eval_cfg_eff, lang)
            lines += [f"## {t}", "", summary_text, "", bucket_text, ""]
        else:
            content = text.strip() or empty_text
            lines += [f"## {t}", "", content, ""]
    return "\n".join(lines)


def report_stem(incremental: bool, lang: str = "zh") -> str:
    """报告基名：zh 沿用原名（analysis / YYYY-MM-DD），en 加 .en 后缀。"""
    suffix = ".en" if lang == "en" else ""
    return (time.strftime("%Y-%m-%d") if incremental else "analysis") + suffix


def write_report(report: str, incremental: bool, lang: str = "zh") -> None:
    """报告落盘：增量写入当日归档 data/analysis/YYYY-MM-DD[.en].md（每天一份，
    同日多次运行刷新当天）；全量写入 analysis[.en].md 作为总览基线。"""
    target = REPORT_DIR / f"{report_stem(incremental, lang)}.md"
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        f.write(report + "\n")
    print(_t(lang, "report_written", path=target.resolve()))


# ---------- CLI ----------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="analyzer.py",
        description=(
            "拾贝分析（单一入口）：自动爬取所需数据 → LLM 提炼 → 输出报告并打印绝对路径。\n"
            "Shibei analysis (single entry): auto crawl → LLM distill → report; prints the absolute path."
        ),
    )
    parser.add_argument("--full", action="store_true", help="强制全量分析（重跑全部帖子）/ force full re-analysis")
    parser.add_argument(
        "--lang",
        choices=LANGS,
        default=None,
        help=(
            "报告与日志语言（zh/en，默认 ANALYZE_LANG → 系统语言 → zh）/ "
            "language of report & logs (default: ANALYZE_LANG → system language → zh)"
        ),
    )
    return parser


def _enabled_sources(config: dict[str, Any]) -> list[str]:
    return [name for name, conf in config.get("sources", {}).items() if conf.get("enabled", True)]


def _source_has_data(name: str) -> bool:
    """某来源目录下是否存在任何帖子 JSON。"""
    source_dir = DATA_DIR / name
    return source_dir.is_dir() and any(source_dir.rglob("*.json"))


def _has_data(config: dict[str, Any]) -> bool:
    """配置的 enabled 来源下是否存在任何帖子 JSON。"""
    return any(_source_has_data(name) for name in _enabled_sources(config))


def _load_topics(
    config: dict[str, Any], since_by_source: dict[str, int | None]
) -> tuple[list[Post], dict[str, set[str]]]:
    """按来源加载帖子；返回 (帖子列表, {来源: 节点集合})。"""
    topics: list[Post] = []
    source_nodes: dict[str, set[str]] = {}
    for name in _enabled_sources(config):
        since = since_by_source.get(name)
        source_dir = DATA_DIR / name
        if not source_dir.is_dir():
            continue
        for node_dir in sorted(source_dir.iterdir()):
            if not node_dir.is_dir():
                continue
            node_posts = load_topics(node_dir, since=since)
            if node_posts:
                source_nodes.setdefault(name, set()).add(node_dir.name)
            topics.extend(node_posts)
    return topics, source_nodes


def _prefer_lang(paths: list[Path], lang: str) -> list[Path]:
    """把同语言的报告文件排到列表末尾（"最新"优先语言一致）。"""
    suffix = f".{lang}"
    return [p for p in paths if not p.stem.endswith(suffix)] + [p for p in paths if p.stem.endswith(suffix)]


def _show_no_new(config: dict[str, Any], lang: str = "zh") -> None:
    # 提示最近一份报告：优先最新每日归档（YYYY-MM-DD[.en]，语言优先），其次全量总览
    stem_re = re.compile(rf"^\d{{4}}-\d{{2}}-\d{{2}}(\.{'|'.join(LANGS)})?$")
    dailies = sorted(
        (p for p in REPORT_DIR.glob("*.md") if stem_re.fullmatch(p.stem)),
        key=lambda p: p.stem,
    )
    dailies = _prefer_lang(dailies, lang)
    full = REPORT_DIR / f"analysis{'.en' if lang == 'en' else ''}.md"
    if not full.exists():
        full = REPORT_DIR / ("analysis.md" if lang == "en" else "analysis.en.md")
    target = dailies[-1] if dailies else (full if full.exists() else None)
    if target is not None:
        print(_t(lang, "no_new_latest", path=target.resolve()))
        return
    if _has_data(config):
        print(_t(lang, "no_new_plain"))
    else:
        print(_t(lang, "no_posts"))
        print(_t(lang, "no_posts_hint"))


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except KeyboardInterrupt:
        # 优雅退出：os._exit 绕过解释器对非守护工作线程的 join，立即结束；
        # 数据均原子写，已爬取/分析内容不丢，下次运行自动继续。
        print(_t(_LANG, "interrupted"), file=sys.stderr, flush=True)
        os._exit(130)


def _main(argv: list[str] | None = None) -> int:
    global _LANG
    args = build_parser().parse_args(argv)
    _LANG = resolve_lang(args.lang)
    check_env()  # 校验 OPENAI_API_KEY，缺失即退出（提示按 _LANG）
    config = load_config()
    _LLM.update(resolve_llm_config(config, _LANG))

    # 自动爬取：某来源数据为空时重置其状态，让增量爬取退化为全量；否则只抓新增。
    # 记录 freshly_full —— 该来源刚被全量爬取，last_crawl 已被更新为当前时刻，
    # 若仍以其为 since，刚爬下来的帖子（created 均早于此刻）会被全部过滤掉。
    state = load_state()
    freshly_full: set[str] = set()
    for name in _enabled_sources(config):
        if not _source_has_data(name):
            freshly_full.add(name)
            state.pop(name, None)
    if freshly_full:
        save_state(state)
        print(_t(_LANG, "first_run_full"))
    run_crawl(config, today=True, lang=_LANG)

    # 加载待分析帖子
    state = load_state()
    since_by_source: dict[str, int | None] = {}
    for name in _enabled_sources(config):
        if args.full or name in freshly_full:
            since_by_source[name] = None
        else:
            since_by_source[name] = state.get(name, {}).get("last_analysis") or state.get(name, {}).get("last_crawl")
    incremental = not args.full
    topics, source_nodes = _load_topics(config, since_by_source)

    if not topics:
        _show_no_new(config, _LANG)
        return 0

    summary = ", ".join(f"{name}({', '.join(sorted(nodes))})" for name, nodes in source_nodes.items())
    print(_t(_LANG, "posts_to_analyze", n=len(topics), summary=summary))
    eval_cfg = load_evaluation(config)
    eval_enabled = bool(eval_cfg.get("enabled", True))
    reset_token_usage()
    merged = analyze(
        topics,
        incremental=incremental,
        language=_LANG,
        eval_enabled=eval_enabled,
        eval_cfg=eval_cfg,
    )

    day = time.strftime("%Y-%m-%d")
    title = day if incremental else _t(_LANG, "full_title", day=day)
    report = build_report(merged, len(topics), summary, title, _LANG, eval_cfg=eval_cfg)
    write_report(report, incremental=incremental, lang=_LANG)

    now = int(time.time())
    for name in source_nodes:
        state.setdefault(name, {})["last_analysis"] = now
    save_state(state)

    print("\n" + report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
