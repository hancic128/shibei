import hashlib
import http.client
import io
import json
import urllib.error

import pytest

import analyzer
from models import MAX_REPLIES, Post, Reply


def _post(id: int, replies: int = 0) -> Post:
    return Post(
        id=str(id),
        source="v2ex",
        node="python",
        title=f"标题{id}",
        content="正文" * 300,  # 600 字符，测试截断到 500
        author="u",
        created=100 + id,
        replies_count=replies,
        url=f"https://www.v2ex.com/t/{id}",
        reply_list=[
            Reply(id=f"r{i}", author="a", content="回复" * 150, created=i)
            for i in range(replies)  # 300 字符 → 截断到 200
        ],
    )


def _today_name() -> str:
    """增量报告按日归档名 YYYY-MM-DD.md（与 analyzer.write_report 一致）。"""
    return analyzer.time.strftime("%Y-%m-%d") + ".md"


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(analyzer, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(analyzer, "CACHE_DIR", tmp_path / "data" / ".cache")
    monkeypatch.setattr(analyzer, "STATE_FILE", tmp_path / "data" / "state.json")
    monkeypatch.setattr(analyzer, "REPORT_DIR", tmp_path / "data" / "analysis")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://fake/v1")  # URL 必填
    monkeypatch.setenv("ANALYZE_MODEL", "test-model")  # 模型必填
    monkeypatch.setenv("ANALYZE_LANG", "zh")  # 语言固定中文，避免断言随宿主机系统语言漂移
    return tmp_path


# ---------- format ----------


def test_format_post_truncates_content_and_replies():
    p = _post(1, replies=15)  # 回复 > MAX_REPLIES，应只保留前 10 条
    text = analyzer.format_post(p)
    assert "## [#1] 标题1" in text
    content_full = "正文" * 300  # 600 字符
    assert content_full[:500] in text
    assert content_full not in text  # 已截断到 500 字符
    assert text.count("  - a:") == MAX_REPLIES
    assert "回复" * 100 in text  # 截断后的 200 字符
    assert "回复" * 150 not in text  # 每条回复截断 200 字符


def test_build_batch_prompt_contains_marker_rule():
    prompt = analyzer.build_batch_prompt("批次文本", idx=0, total=2, title="痛点机会", desc="定义")
    assert "只提炼「痛点机会」类信息" in prompt
    assert "[#帖子ID]" in prompt
    assert "（第 1/2 批）" in prompt
    assert "批次文本" in prompt


def test_build_batch_prompt_forces_chinese_even_for_english():
    # 数据源有英文（HN / Lobste.rs / Dev.to / Product Hunt），结论必须仍是中文
    prompt = analyzer.build_batch_prompt("Hello world", idx=0, total=1, title="Trend", desc="def")
    assert "一律用中文回答" in prompt
    assert "原文是英文" in prompt


def test_build_merge_prompt_forces_chinese_even_for_english():
    prompt = analyzer.build_merge_prompt(["English result"], incremental=False)
    assert "一律用中文回答" in prompt
    assert "原文是英文" in prompt


# ---------- merge ----------


def test_merge_results_hierarchical(monkeypatch):
    # 7 个结果 → [abc][def][g] → 首轮 2 次合并，余 1 个直通 → 再 1 次合并 = 3 次调用
    calls = []
    monkeypatch.setattr(analyzer, "call_api", lambda prompt, **kw: calls.append(prompt) or "merged")
    result = analyzer.merge_results([f"r{i}" for i in range(7)], incremental=False)
    assert result == "merged"
    assert len(calls) == 3


def test_merge_results_single_no_call(monkeypatch):
    monkeypatch.setattr(analyzer, "call_api", lambda *a, **kw: pytest.fail("不应调用 LLM"))
    assert analyzer.merge_results(["only"], incremental=False) == "only"
    assert analyzer.merge_results([], incremental=False) == ""


# ---------- 链接还原 ----------


def test_restore_links():
    id2link = {"1": ("标题1", "https://v2ex.com/t/1")}
    text = "洞察 [#1] 与 [#2]"
    out = analyzer.restore_links(text, id2link)
    assert out == "洞察 [来源](https://v2ex.com/t/1) 与 [#2]"  # 未知 ID 保留原文


def test_restore_links_unknown_warns(capsys):
    analyzer.restore_links("[#999]", {})
    assert "未找到帖子 999" in capsys.readouterr().err


# ---------- analyze ----------


def test_analyze_end_to_end(env, monkeypatch):
    # 20 帖 → 1 批（BATCH_SIZE=20）：1 次「一次输出多类」调用 + 3 次「合并+分组」调用
    topics = [_post(i) for i in range(20)]
    calls = {"count": 0}

    def fake_call(prompt, **kw):
        calls["count"] += 1
        if "整理" in prompt:  # 「合并去重并分组整理」或「只做整理分组」
            return "### 分类A\n- [#1] 洞察"
        return "## 产品创意\n- [#1] 洞察\n## 用户痛点\n- [#1] 洞察\n## 潜在机会\n- [#1] 洞察"

    monkeypatch.setattr(analyzer, "call_api", fake_call)

    merged = analyzer.analyze(topics, incremental=False)
    assert set(merged) == {"产品创意", "用户痛点", "潜在机会"}
    for text in merged.values():
        assert "### 分类A" in text  # 分组结构保留
        assert "[来源](https://www.v2ex.com/t/1)" in text  # [#1] 已还原为来源链接
    assert calls["count"] == 1 + 3  # 1 批多类 + 3 类合并分组
    # 缓存已清理
    assert not list(analyzer.CACHE_DIR.glob("*.json"))


def test_analyze_cache_hit(env, monkeypatch):
    # 预写该 run_id 的批次缓存（一次调用输出多类）→ 批次全命中，只跑每类合并分组
    topics = [_post(i) for i in range(20)]  # 1 批 = 1 个缓存文件
    run_id = hashlib.md5("".join(p.id for p in topics).encode("utf-8")).hexdigest()[:12]
    analyzer.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (analyzer.CACHE_DIR / f"{run_id}_b0_multi_zh.json").write_text(
        json.dumps({"result": {key: "[#1] 洞察" for key, _, _ in analyzer.CATEGORIES}}),
        encoding="utf-8",
    )

    calls = {"count": 0}
    monkeypatch.setattr(
        analyzer,
        "call_api",
        lambda prompt, **kw: (
            calls.__setitem__("count", calls["count"] + 1),
            "### 分类A\n- [#1] 洞察",
        )[1],
    )
    analyzer.analyze(topics)
    assert calls["count"] == 3  # 仅 3 类合并分组（批次分析全命中缓存）


def test_analyze_falls_back_to_per_category_when_unparsed(env, monkeypatch):
    # 多类输出未按 `## 类别` 分节（无法解析）→ 回退为按类逐次调用（保底路径）
    topics = [_post(i) for i in range(5)]
    calls = {"count": 0}
    monkeypatch.setattr(
        analyzer,
        "call_api",
        lambda prompt, **kw: (calls.__setitem__("count", calls["count"] + 1), "[#1] 洞察")[1],
    )
    analyzer.analyze(topics)
    assert calls["count"] == 1 + 3 + 3  # 1 次多类调用（解析失败）+ 回退按类 3 次 + 3 类合并分组


def test_build_multi_prompt_lists_all_categories():
    prompt = analyzer.build_multi_prompt("批次文本", idx=0, total=2)
    for _, title, _ in analyzer.CATEGORIES:
        assert f"## {title}" in prompt
    assert "[#帖子ID]" in prompt
    assert "（第 1/2 批）" in prompt
    assert "批次文本" in prompt


def test_parse_multi_splits_sections_and_drops_empty():
    raw = "## 产品创意\n- [#1] 洞察\n## 用户痛点\n无\n## 潜在机会\n\n- [#2] 机会"
    out = analyzer.parse_multi(raw)
    assert out["ideas"] == "- [#1] 洞察"
    assert out["pain"] == ""  # 「无」视为空
    assert out["indie"] == "- [#2] 机会"


def test_parse_multi_ignores_unknown_sections():
    out = analyzer.parse_multi("## 别的标题\n- x\n正文")
    assert set(out.values()) == {""}


def test_consolidate_single_result_organizes(monkeypatch):
    # 单批结果交给分组整理（organize_topics）
    monkeypatch.setattr(analyzer, "call_api", lambda prompt, **kw: "### 分类A\n- [#1] x")
    out = analyzer.consolidate(["- [#1] x"], "产品创意", incremental=True)
    assert out.startswith("### 分类A")


def test_consolidate_keeps_content_when_all_calls_fail(monkeypatch):
    # 合并分组与回退路径都失败 → 原样拼接各批结果，不丢内容
    def boom(prompt, **kw):
        raise RuntimeError("llm down")

    monkeypatch.setattr(analyzer, "call_api", boom)
    out = analyzer.consolidate(["- [#1] x", "- [#2] y"], "产品创意", incremental=False)
    assert "[#1] x" in out and "[#2] y" in out


# ---------- load_topics ----------


def test_load_topics_since_and_bad_file(env):
    node = analyzer.DATA_DIR / "v2ex" / "python"
    node.mkdir(parents=True)
    (node / "1.json").write_text(json.dumps(_post(1).to_dict()), encoding="utf-8")
    (node / "2.json").write_text(json.dumps(_post(2).to_dict()), encoding="utf-8")
    (node / "bad.json").write_text("{not json", encoding="utf-8")  # 应跳过

    # post1.created=101, post2.created=102 → since=102 只留 post2
    posts = analyzer.load_topics(node, since=102)
    assert [p.id for p in posts] == ["2"]  # 文件名排序 + since 过滤 + 坏文件跳过


# ---------- LLM 配置 ----------


def test_resolve_llm_config_url_required(monkeypatch):
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("ANALYZE_MODEL", raising=False)
    # URL 未提供 → 必须显式配置，缺失即退出
    with pytest.raises(SystemExit):
        analyzer.resolve_llm_config({})
    # URL 有了但模型缺失 → 也要退出
    with pytest.raises(SystemExit):
        analyzer.resolve_llm_config({"llm": {"base_url": "https://cfg.example/v1"}})


def test_resolve_llm_config_model_required(monkeypatch):
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("ANALYZE_MODEL", raising=False)
    with pytest.raises(SystemExit):
        analyzer.resolve_llm_config({"llm": {"base_url": "https://cfg.example/v1"}})
    # 模型缺省不再兜底 gpt-4o-mini


def test_resolve_llm_config_priority(monkeypatch):
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("ANALYZE_MODEL", raising=False)
    monkeypatch.delenv("ANALYZE_MAX_TOKENS", raising=False)

    # config.json 提供 URL / model / max_tokens
    cfg = {"llm": {"base_url": "https://cfg.example/v1/", "model": "cfg-model", "max_tokens": 2048}}
    assert analyzer.resolve_llm_config(cfg) == {
        "base_url": "https://cfg.example/v1",
        "model": "cfg-model",
        "max_tokens": "2048",
    }

    # max_tokens 可选，兜底默认
    assert analyzer.resolve_llm_config({"llm": {"base_url": "https://cfg.example/v1", "model": "m"}}) == {
        "base_url": "https://cfg.example/v1",
        "model": "m",
        "max_tokens": "4096",
    }

    # 环境变量覆盖 config.json
    monkeypatch.setenv("OPENAI_BASE_URL", "https://env.example/v1")
    monkeypatch.setenv("ANALYZE_MODEL", "env-model")
    monkeypatch.setenv("ANALYZE_MAX_TOKENS", "8192")
    assert analyzer.resolve_llm_config(cfg) == {
        "base_url": "https://env.example/v1",
        "model": "env-model",
        "max_tokens": "8192",
    }


# ---------- call_api ----------


def _fake_http_error(code: int, message: str):
    body = json.dumps({"error": {"message": message}}).encode()
    hdrs = http.client.HTTPMessage()
    return urllib.error.HTTPError("http://fake/v1/chat/completions", code, "Error", hdrs, io.BytesIO(body))


def test_call_api_400_raises_with_detail_no_retry(monkeypatch):
    calls = []

    def fake_urlopen(req, **kw):
        calls.append(req)
        raise _fake_http_error(400, "model not found")

    monkeypatch.setattr(analyzer.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(analyzer, "_LLM", {"base_url": "http://fake/v1", "model": "m", "max_tokens": "4096"})
    with pytest.raises(RuntimeError, match="model not found"):
        analyzer.call_api("hi", retries=3)
    assert len(calls) == 1  # 4xx 不重试


def test_call_api_429_retries_then_raises(monkeypatch):
    calls = []

    def fake_urlopen(req, **kw):
        calls.append(req)
        raise _fake_http_error(429, "rate limited")

    monkeypatch.setattr(analyzer.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(analyzer, "_LLM", {"base_url": "http://fake/v1", "model": "m", "max_tokens": "4096"})
    monkeypatch.setattr(analyzer.time, "sleep", lambda _s: None)
    with pytest.raises(RuntimeError, match="429"):
        analyzer.call_api("hi", retries=2)
    assert len(calls) == 3  # 429 可重试


def test_check_env_missing(monkeypatch, capsys):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        analyzer.check_env()
    assert "OPENAI_API_KEY" in capsys.readouterr().err


# ---------- main ----------


def _make_post_file(dir_path, post: Post):
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / f"{post.id}.json").write_text(json.dumps(post.to_dict()), encoding="utf-8")


def test_main_writes_report_and_state(env, monkeypatch, capsys):
    monkeypatch.setattr(
        analyzer,
        "load_config",
        lambda: {"sources": {"v2ex": {"enabled": True, "nodes": ["python"]}}, "llm": {}},
    )
    monkeypatch.setattr(analyzer, "call_api", lambda prompt, **kw: "[#1] 洞察")
    monkeypatch.setattr(analyzer, "run_crawl", lambda *a, **kw: 0)  # 自动爬取：测试中不真实联网
    _make_post_file(analyzer.DATA_DIR / "v2ex" / "python", _post(1))

    rc = analyzer.main([])  # 默认：增量 → 当日归档 YYYY-MM-DD.md
    assert rc == 0

    report = (analyzer.REPORT_DIR / _today_name()).read_text(encoding="utf-8")
    assert f"# {_today_name().removesuffix('.md')}" in report
    assert "来源: v2ex(python)" in report
    assert "## 产品创意" in report
    assert "[来源](https://www.v2ex.com/t/1)" in report  # 链接还原生效

    # 打印报告的绝对路径
    abs_path = str((analyzer.REPORT_DIR / _today_name()).resolve())
    assert abs_path in capsys.readouterr().out

    state = json.loads(analyzer.STATE_FILE.read_text(encoding="utf-8"))
    assert "last_analysis" in state["v2ex"]


def test_main_full_writes_full_report(env, monkeypatch):
    monkeypatch.setattr(
        analyzer,
        "load_config",
        lambda: {"sources": {"v2ex": {"enabled": True, "nodes": ["python"]}}, "llm": {}},
    )
    monkeypatch.setattr(analyzer, "call_api", lambda prompt, **kw: "[#1] 洞察")
    monkeypatch.setattr(analyzer, "run_crawl", lambda *a, **kw: 0)
    _make_post_file(analyzer.DATA_DIR / "v2ex" / "python", _post(1))

    rc = analyzer.main(["--full"])  # 全量 → analysis.md
    assert rc == 0
    assert (analyzer.REPORT_DIR / "analysis.md").exists()
    state = json.loads(analyzer.STATE_FILE.read_text(encoding="utf-8"))
    assert "last_analysis" in state["v2ex"]


def test_main_empty_data_auto_full_crawl(env, monkeypatch, capsys):
    # 数据为空 → 应重置状态并走全量（run_crawl 被调用且 today=True）
    calls = {}
    monkeypatch.setattr(
        analyzer,
        "load_config",
        lambda: {"sources": {"v2ex": {"enabled": True, "nodes": ["python"]}}, "llm": {}},
    )
    monkeypatch.setattr(analyzer, "call_api", lambda prompt, **kw: "[#1] 洞察")
    monkeypatch.setattr(
        analyzer,
        "run_crawl",
        lambda *a, **kw: calls.update(kw) or 0,
    )
    rc = analyzer.main([])
    assert rc == 0
    assert calls["today"] is True
    assert "自动全量爬取" in capsys.readouterr().out


def test_main_empty_data_full_crawl_still_analyzes(env, monkeypatch, capsys):
    # 数据为空 → 全量爬取后 last_crawl 被更新为当前时刻，
    # 刚爬下来的帖子（created 早于此刻）不应被 since 过滤掉，必须照常分析并出报告。
    monkeypatch.setattr(
        analyzer,
        "load_config",
        lambda: {"sources": {"v2ex": {"enabled": True, "nodes": ["python"]}}, "llm": {}},
    )
    monkeypatch.setattr(analyzer, "call_api", lambda prompt, **kw: "[#1] 洞察")

    def fake_run_crawl(config, source_name=None, today=False, lang="zh"):
        # 模拟真实爬虫：写入帖子文件，并把 last_crawl 置为当前时刻
        _make_post_file(analyzer.DATA_DIR / "v2ex" / "python", _post(1))
        state = analyzer.load_state()
        state.setdefault("v2ex", {})["last_crawl"] = int(analyzer.time.time())
        analyzer.save_state(state)
        return 1

    monkeypatch.setattr(analyzer, "run_crawl", fake_run_crawl)
    rc = analyzer.main([])
    assert rc == 0
    report = (analyzer.REPORT_DIR / _today_name()).read_text(encoding="utf-8")
    assert "基于 1 个帖子自动生成" in report
    assert "没有新增帖子" not in capsys.readouterr().out


def test_main_mixed_empty_source_still_analyzes(env, monkeypatch, capsys):
    # 来源 a 已有数据与 last_analysis，来源 b 为空：b 被全量爬取后 last_crawl=当前时刻，
    # b 刚爬下来的帖子（created 早于此刻）不应被 since 过滤掉；a 仍按增量处理。
    monkeypatch.setattr(
        analyzer,
        "load_config",
        lambda: {
            "sources": {
                "a": {"enabled": True, "nodes": ["x"]},
                "b": {"enabled": True, "nodes": ["y"]},
            },
            "llm": {},
        },
    )
    monkeypatch.setattr(analyzer, "call_api", lambda prompt, **kw: "[#1] 洞察")
    _make_post_file(analyzer.DATA_DIR / "a" / "x", _post(1))  # a 已有数据
    analyzer.save_state({"a": {"last_crawl": 100, "last_analysis": 200}})  # a 增量点

    def fake_run_crawl(config, source_name=None, today=False, lang="zh"):
        # 只给空的来源 b 写帖子，并把 last_crawl 置为当前时刻；a 不新增
        _make_post_file(analyzer.DATA_DIR / "b" / "y", _post(2))
        st = analyzer.load_state()
        st.setdefault("b", {})["last_crawl"] = int(analyzer.time.time())
        analyzer.save_state(st)
        return 1

    monkeypatch.setattr(analyzer, "run_crawl", fake_run_crawl)
    rc = analyzer.main([])
    assert rc == 0
    report = (analyzer.REPORT_DIR / _today_name()).read_text(encoding="utf-8")
    assert "基于 1 个帖子自动生成" in report  # 只有 b 的新帖被分析，a 无新增
    assert "没有新增帖子" not in capsys.readouterr().out


def test_main_no_topics(env, monkeypatch, capsys):
    monkeypatch.setattr(analyzer, "load_config", lambda: {"sources": {"v2ex": {"enabled": True}}, "llm": {}})
    monkeypatch.setattr(analyzer, "run_crawl", lambda *a, **kw: 0)
    rc = analyzer.main([])
    assert rc == 0
    assert "没有可分析的帖子" in capsys.readouterr().out


def test_main_interrupt_exits_130(env, monkeypatch, capsys):
    def boom(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(analyzer, "run_crawl", boom)
    monkeypatch.setattr(analyzer.os, "_exit", lambda code: (_ for _ in ()).throw(SystemExit(code)))
    with pytest.raises(SystemExit) as e:
        analyzer.main([])
    assert e.value.code == 130
    assert "已中断" in capsys.readouterr().err


def test_main_missing_key_exits(env, monkeypatch, capsys):
    monkeypatch.setattr(analyzer, "load_config", lambda: {"sources": {}, "llm": {}})
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        analyzer.main([])


# ---------- 报告子主题分组（organize_topics） ----------


def test_organize_topics_groups_into_h3(monkeypatch):
    # LLM 正常返回带 ### 的分组文本 → 原样保留（报告按 H3 子主题分节）
    monkeypatch.setattr(
        analyzer,
        "call_api",
        lambda prompt, **kw: "### 效率工具\n- [#1] 洞察A\n### 生态\n- [#2] 洞察B",
    )
    out = analyzer.organize_topics("some merged text", "产品创意")
    assert "### 效率工具" in out
    assert "### 生态" in out
    assert "[#2] 洞察B" in out


def test_organize_topics_empty_input_returns_as_is(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("空输入不应触发 LLM 调用")

    monkeypatch.setattr(analyzer, "call_api", boom)
    assert analyzer.organize_topics("", "产品创意") == ""
    assert analyzer.organize_topics("   \n", "产品创意") == "   \n"


def test_organize_topics_fallback_on_error(monkeypatch):
    # LLM 异常 → 回退原样文本，不影响报告
    def boom(*a, **kw):
        raise RuntimeError("llm down")

    monkeypatch.setattr(analyzer, "call_api", boom)
    src = "- [#1] 洞察A"
    assert analyzer.organize_topics(src, "产品创意") == src


def test_organize_topics_fallback_on_empty_reply(monkeypatch):
    monkeypatch.setattr(analyzer, "call_api", lambda prompt, **kw: "  ")
    src = "- [#1] 洞察A"
    assert analyzer.organize_topics(src, "产品创意") == src


def test_build_organize_prompt_keeps_marker_rule():
    prompt = analyzer.build_organize_prompt("内容", "痛点机会")
    assert "只做整理分组" in prompt
    assert "[#帖子ID]" in prompt
    assert "### 子主题名" in prompt


# ---------- 英文报告（--lang en / ANALYZE_LANG=en） ----------


def _clear_lang_env(monkeypatch) -> None:
    """清掉语言相关环境变量与 locale 设置，让默认语言只取决于显式配置。"""
    for key in ("ANALYZE_LANG", "LC_ALL", "LC_MESSAGES", "LANG"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(analyzer.locale, "getlocale", lambda: (None, None))


def test_resolve_lang_priority(monkeypatch):
    _clear_lang_env(monkeypatch)
    assert analyzer.resolve_lang(None) == "zh"  # 无配置且无系统语言 → 中文
    assert analyzer.resolve_lang("en") == "en"
    assert analyzer.resolve_lang("fr") == "zh"  # 非法值回落默认
    monkeypatch.setenv("ANALYZE_LANG", "en")
    assert analyzer.resolve_lang(None) == "en"
    assert analyzer.resolve_lang("zh") == "zh"  # --lang 优先于环境变量


def test_resolve_lang_follows_system_locale(monkeypatch):
    """默认语言跟随系统语言：en* → en，其余（含 C/POSIX）→ zh。"""
    _clear_lang_env(monkeypatch)
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    assert analyzer.resolve_lang(None) == "en"
    monkeypatch.setenv("LC_ALL", "en_GB.UTF-8")
    monkeypatch.setenv("LANG", "zh_CN.UTF-8")
    assert analyzer.resolve_lang(None) == "en"  # LC_ALL 优先于 LANG
    monkeypatch.delenv("LC_ALL")
    assert analyzer.resolve_lang(None) == "zh"
    monkeypatch.delenv("LANG")
    monkeypatch.setenv("LANG", "C")
    assert analyzer.resolve_lang(None) == "zh"
    monkeypatch.setenv("ANALYZE_LANG", "zh")
    monkeypatch.setattr(analyzer.locale, "getlocale", lambda: ("en_US", "UTF-8"))
    assert analyzer.resolve_lang(None) == "zh"  # ANALYZE_LANG 仍优先于系统语言


def test_resolve_lang_falls_back_to_locale_setting(monkeypatch):
    """环境变量缺失时读 locale.getlocale()（如程序内 setlocale 后）。"""
    _clear_lang_env(monkeypatch)
    monkeypatch.setattr(analyzer.locale, "getlocale", lambda: ("en_US", "UTF-8"))
    assert analyzer.resolve_lang(None) == "en"


def test_english_categories_and_prompt():
    assert [t for _, t, _ in analyzer._categories("en")] == ["Product Ideas", "User Pain Points", "Opportunities"]
    prompt = analyzer.build_multi_prompt("batch text", idx=0, total=1, lang="en")
    for _, title, _ in analyzer._categories("en"):
        assert f"## {title}" in prompt
    assert "Answer in English" in prompt
    assert "产品创意" not in prompt


# ---------- 合并 prompt 空输入守卫 ----------


def test_merge_prompt_rejects_empty_results():
    """空列表会发出空 body prompt 给 LLM，直接拒绝让上层走 empty_text 分支。"""
    with pytest.raises(ValueError, match="至少 1 个非空批次结果"):
        analyzer.build_merge_prompt([], incremental=True)
    with pytest.raises(ValueError, match="至少 1 个非空批次结果"):
        analyzer.build_merge_prompt([], incremental=False, lang="en")


def test_consolidate_prompt_rejects_empty_results():
    with pytest.raises(ValueError, match="至少 1 个非空批次结果"):
        analyzer.build_consolidate_prompt([], "产品创意", incremental=True)
    with pytest.raises(ValueError, match="至少 1 个非空批次结果"):
        analyzer.build_consolidate_prompt([], "Product Ideas", incremental=False, lang="en")


def test_merge_prompt_accepts_non_empty_results():
    """非空列表（含空字符串元素的边界）仍正常返回 prompt——守卫只挡空列表，不挡含空元素的列表。"""
    prompt = analyzer.build_merge_prompt(["条目一 — [#1]\n条目二 — [#2]"], incremental=True)
    assert "条目一 — [#1]" in prompt
    assert "今日新增" in prompt
    prompt_en = analyzer.build_merge_prompt(["item — [#1]"], incremental=False, lang="en")
    assert "item — [#1]" in prompt_en
    assert "today's incremental" not in prompt_en


def test_consolidate_prompt_accepts_non_empty_results():
    prompt = analyzer.build_consolidate_prompt(["条目 — [#1]"], "产品创意", incremental=True)
    assert "条目 — [#1]" in prompt
    assert "产品创意" in prompt
    prompt_en = analyzer.build_consolidate_prompt(["item — [#1]"], "Product Ideas", incremental=False, lang="en")
    assert "Product Ideas" in prompt_en


def test_merge_and_consolidate_normalize_empty_via_entry_filter():
    """上游入口 consolidate/merge_results 已先过滤空结果，不会把空列表传给 prompt 构造函数。"""
    # consolidate：results 全是空字符串时被入口过滤后返回 ""，不应触发 prompt 守卫
    assert analyzer.consolidate(["", "  ", "无"], "产品创意", incremental=True, lang="zh") == ""
    # merge_results：同样过滤后返回 ""
    assert analyzer.merge_results(["", "无"], incremental=True, lang="zh") == ""


def test_is_empty_result_by_language():
    assert analyzer.is_empty_result("无", "zh")
    assert analyzer.is_empty_result("None", "en")
    assert analyzer.is_empty_result("  nothing. ", "en")
    assert not analyzer.is_empty_result("- item", "en")


def test_parse_multi_english_sections():
    raw = "## Product Ideas\n- [#1] insight\n## User Pain Points\nNone\n## Opportunities\n\n- [#2] chance"
    out = analyzer.parse_multi(raw, "en")
    assert out["ideas"].startswith("- [#1]")
    assert out["pain"] == ""  # 「None」视为空
    assert out["indie"].startswith("- [#2]")


def test_build_report_english():
    report = analyzer.build_report(
        {"Product Ideas": "- thing — [来源](https://x)", "User Pain Points": ""},
        3,
        "v2ex(python)",
        "2026-09-10",
        "en",
    )
    assert report.startswith("# 2026-09-10")
    assert "Sources: v2ex(python)" in report
    assert "Generated from 3 posts" in report
    assert "No insights found this round" in report  # 空类别占位


def test_report_stem_by_language(env):
    today = analyzer.time.strftime("%Y-%m-%d")
    assert analyzer.report_stem(incremental=True, lang="zh") == today
    assert analyzer.report_stem(incremental=True, lang="en") == today + ".en"
    assert analyzer.report_stem(incremental=False, lang="zh") == "analysis"
    assert analyzer.report_stem(incremental=False, lang="en") == "analysis.en"


def test_write_report_english_files(env):
    analyzer.write_report("# English daily", incremental=True, lang="en")
    analyzer.write_report("# English full", incremental=False, lang="en")
    daily = analyzer.REPORT_DIR / (analyzer.time.strftime("%Y-%m-%d") + ".en.md")
    assert daily.read_text(encoding="utf-8").startswith("# English daily")
    assert (analyzer.REPORT_DIR / "analysis.en.md").exists()
    assert not (analyzer.REPORT_DIR / _today_name()).exists()  # 不写中文文件


def test_analyze_english_end_to_end(env, monkeypatch):
    topics = [_post(1), _post(2)]
    monkeypatch.setattr(
        analyzer,
        "call_api",
        lambda prompt, **kw: (
            "## Product Ideas\n- [#1] 洞察\n## User Pain Points\n- [#1] 痛点\n## Opportunities\n- [#2] 机会"
        ),
    )
    merged = analyzer.analyze(topics, incremental=False, language="en")
    assert set(merged) == {"Product Ideas", "User Pain Points", "Opportunities"}
    assert "[来源]" in merged["Product Ideas"]  # 链接还原仍生效
    assert not list(analyzer.CACHE_DIR.glob("*.json"))  # 分析完清理缓存


# ---------- 评估打分（idea-eval 内嵌） ----------


def test_load_evaluation_defaults_when_missing():
    """config 缺 evaluation 段 → 返回默认值。"""
    cfg = analyzer.load_evaluation({})
    assert cfg["enabled"] is True
    assert cfg["keep_threshold"] == 16
    assert cfg["show_watch"] is True
    assert cfg["show_rejected"] is True


def test_load_evaluation_overrides_from_file():
    """config 自带 evaluation 段 → 覆盖默认。"""
    cfg = analyzer.load_evaluation({"evaluation": {"enabled": False, "keep_threshold": 12}})
    assert cfg["enabled"] is False
    assert cfg["keep_threshold"] == 12
    assert cfg["show_watch"] is True  # 未指定 → 默认
    assert cfg["show_rejected"] is True


def test_load_evaluation_ignores_non_dict():
    """evaluation 段若不是 dict（如字符串/null）→ 用默认值兜底。"""
    assert analyzer.load_evaluation({"evaluation": "off"})["enabled"] is True
    assert analyzer.load_evaluation({"evaluation": None})["keep_threshold"] == 16


def test_build_batch_prompt_zh_adds_score_rule_when_enabled():
    prompt = analyzer.build_batch_prompt(
        "批次", idx=0, total=1, title="产品创意", desc="def", lang="zh", eval_enabled=True
    )
    assert "[v=X,d=Y,✓/✗]" in prompt
    assert "价值" in prompt
    assert "难度" in prompt


def test_build_batch_prompt_en_adds_score_rule_when_enabled():
    prompt = analyzer.build_batch_prompt(
        "batch", idx=0, total=1, title="Product Ideas", desc="def", lang="en", eval_enabled=True
    )
    assert "[v=X,d=Y,✓/✗]" in prompt
    assert "value" in prompt
    assert "difficulty" in prompt


def test_build_batch_prompt_omits_score_rule_when_disabled():
    """eval_enabled=False（默认）→ 不附加评分规则（回归保护）。"""
    prompt = analyzer.build_batch_prompt("批次", idx=0, total=1, title="产品创意", desc="def")
    assert "[v=X,d=Y,✓/✗]" not in prompt
    assert "评分必须给具体数字" not in prompt


def test_build_multi_prompt_only_ideas_section_has_score_rule():
    """多类 prompt 中，仅「产品创意」分类 section 附带评估规则；其他分类不受影响。"""
    prompt_zh = analyzer.build_multi_prompt("批次", idx=0, total=1, lang="zh", eval_enabled=True)
    # 找到三个分类的 section 起点
    ideas_pos = prompt_zh.index("## 产品创意")
    pain_pos = prompt_zh.index("## 用户痛点")
    indie_pos = prompt_zh.index("## 潜在机会")
    ideas_block = prompt_zh[ideas_pos:pain_pos]
    pain_block = prompt_zh[pain_pos:indie_pos]
    indie_block = prompt_zh[indie_pos:]
    assert "[v=X,d=Y,✓/✗]" in ideas_block  # 产品创意含评分规则
    assert "[v=X,d=Y,✓/✗]" not in pain_block  # 用户痛点不含
    assert "[v=X,d=Y,✓/✗]" not in indie_block  # 潜在机会不含


def test_build_multi_prompt_omits_score_rule_when_disabled():
    prompt = analyzer.build_multi_prompt("批次", idx=0, total=1, lang="zh")  # eval_enabled 默认 False
    assert "[v=X,d=Y,✓/✗]" not in prompt


def test_parse_item_score_valid():
    v, d, r, rest = analyzer.parse_item_score("- [v=4,d=2,✓] PII 脱敏 — [#1]")
    assert v == 4
    assert d == 2
    assert r == "✓"
    assert "PII 脱敏" in rest and "[#" not in rest.split("—")[0]  # 评分字段已被剥离


def test_parse_item_score_handles_concern():
    v, d, r, rest = analyzer.parse_item_score("- [v=3,d=2,✗] 涉合规 — [#2]")
    assert r == "✗"
    assert v == 3


def test_parse_item_score_tolerates_spaces_in_tag():
    """部分模型（minmax m3 等）输出 `[v=4, d=2, ✓]` 带空格，应被正常解析。"""
    v, d, r, rest = analyzer.parse_item_score("- [v=4, d=2, ✓] PII 脱敏 — [#1]")
    assert (v, d, r) == (4, 2, "✓")
    assert "PII 脱敏" in rest


def test_parse_item_score_invalid_returns_zero():
    v, d, r, line = analyzer.parse_item_score("- 普通条目，没有评分 — [#1]")
    assert (v, d, r) == (0, 0, "?")
    assert line == "- 普通条目，没有评分 — [#1]"


def test_categorize_items_splits_keep_watch_rejected():
    text = (
        "- [v=4,d=2,✓] 高价值低难度 — [#1]\n"
        "- [v=5,d=5,✓] 高价值高难度 — [#2]\n"
        "- [v=2,d=4,✓] 低价值 — [#3]\n"
        "- [v=3,d=2,✗] 红线命中 — [#4]\n"
        "- 普通条目无评分 — [#5]\n"
        "### H3 小节\n"
        "- 非条目行\n"
    )
    buckets = analyzer.categorize_items(text, {"keep_threshold": 16})
    # 总分: [4*4=16, 5*1=5, 2*2=4, 0, 0]
    assert len(buckets["keep"]) == 1
    assert len(buckets["watch"]) == 2  # 总分 5 和 4 的两条
    assert len(buckets["rejected"]) == 1
    assert len(buckets["unscored"]) == 1
    # keep 第一条是 16 分
    assert buckets["keep"][0][0] == 4 and buckets["keep"][0][1] == 2
    # watch 按总分降序
    watch_scores = [analyzer._total_score(v, d) for v, d, _, _ in buckets["watch"]]
    assert watch_scores == sorted(watch_scores, reverse=True)
    # rejected 不被排序影响但保留
    assert buckets["rejected"][0][2] == "✗"
    # unscored 原样保留整行
    assert "普通条目无评分" in buckets["unscored"][0][3]


def test_categorize_items_tolerates_spaced_score_format():
    """m3 类模型输出 `[v=4, d=2, ✓]` 带空格时，分桶与无空格格式一致。"""
    text = "- [v=4, d=2, ✓] 头条 — [#1]\n- [v=3, d=2, ✓] 中等 — [#2]\n- [v=3, d=2, ✗] 红线 — [#3]\n"
    buckets = analyzer.categorize_items(text, {"keep_threshold": 16})
    # 总分: [4*4=16, 3*4=12, 0]
    assert len(buckets["keep"]) == 1
    assert len(buckets["watch"]) == 1
    assert len(buckets["rejected"]) == 1
    assert len(buckets["unscored"]) == 0  # 关键：m3 风格不应落进未评分桶


def test_categorize_items_threshold_boundary():
    """总分恰好等于阈值 → 进入 keep。"""
    text = "- [v=4,d=2,✓] 边界 — [#1]"  # 总分 16
    buckets = analyzer.categorize_items(text, {"keep_threshold": 16})
    assert len(buckets["keep"]) == 1
    assert len(buckets["watch"]) == 0


def test_categorize_items_show_watch_disabled_hides_buckets():
    """show_watch=False 时，watch 与 unscored 桶都不输出（_format_eval_buckets）。"""
    text = "- [v=2,d=4,✓] 待观察 — [#1]\n- 无评分 — [#2]"
    buckets = analyzer.categorize_items(text, {"keep_threshold": 16})
    cfg = {"keep_threshold": 16, "show_watch": False, "show_rejected": True}
    out = analyzer._format_eval_buckets(buckets, cfg, "zh")
    assert "保留清单" not in out
    assert "待观察" not in out  # show_watch=False → 隐藏
    assert "未评分" not in out  # show_watch=False → 也隐藏


def test_categorize_items_show_rejected_disabled_hides_rejected():
    text = "- [v=4,d=2,✗] 否决 — [#1]"
    buckets = analyzer.categorize_items(text, {"keep_threshold": 16})
    cfg = {"keep_threshold": 16, "show_watch": True, "show_rejected": False}
    out = analyzer._format_eval_buckets(buckets, cfg, "zh")
    assert "被否决" not in out


def test_build_report_with_eval_shows_buckets():
    """传入 eval_cfg → 「产品创意」分类按 4 个桶输出，其他分类原样。

    注：build_report 接收的是 analyze 末尾已经 restore_links 还原过的文本，
    因此输入已带 [来源](url) 而不是 [#ID]。
    """
    merged = {
        "产品创意": (
            "- [v=5,d=1,✓] 头条 — [来源](https://x/1)\n"
            "- [v=2,d=4,✓] 待观察 — [来源](https://x/2)\n"
            "- [v=3,d=2,✗] 否决 — [来源](https://x/3)"
        ),
        "用户痛点": "- 普通痛点 — [来源](https://x/4)",
    }
    report = analyzer.build_report(
        merged,
        10,
        "src(x)",
        "2026-09-16",
        "zh",
        eval_cfg={"enabled": True, "keep_threshold": 16, "show_watch": True, "show_rejected": True},
    )
    assert "### 保留清单 (1)" in report
    assert "### 待观察 (1)" in report
    assert "### 被否决 (1)" in report
    assert "### 未评分 (0)" not in report  # 没有未评分条目
    assert "## 产品创意" in report
    assert "## 用户痛点" in report
    assert "- 普通痛点 — [来源](https://x/4)" in report  # 链接还原仍生效（输入已还原）


def test_build_report_without_eval_is_legacy():
    """不传 eval_cfg → 输出与旧格式一致（回归保护）。"""
    merged = {
        "产品创意": "- 普通条目 — [来源](https://x/1)",
        "用户痛点": "- 普通痛点 — [来源](https://x/2)",
    }
    report = analyzer.build_report(merged, 5, "src(x)", "2026-09-16", "zh")
    assert "### 保留清单" not in report
    assert "### 待观察" not in report
    assert "本轮" not in report
    assert "## 产品创意" in report
    assert "- 普通条目 — [来源](https://x/1)" in report


def test_build_report_eval_disabled_via_cfg():
    """eval_cfg['enabled']=False → 等同旧行为（即便传了 eval_cfg）。"""
    merged = {"产品创意": "- [v=4,d=2,✓] 条目 — [来源](https://x/1)"}
    report = analyzer.build_report(
        merged,
        1,
        "x",
        "2026-09-16",
        "zh",
        eval_cfg={"enabled": False, "keep_threshold": 16, "show_watch": True, "show_rejected": True},
    )
    assert "### 保留清单" not in report
    assert "## 产品创意" in report


def test_build_report_eval_english_buckets():
    """英文报告同样支持分桶输出。"""
    merged = {
        "Product Ideas": "- [v=5,d=1,✓] Top — [来源](https://x/1)\n- [v=2,d=4,✓] Watch — [来源](https://x/2)",
        "User Pain Points": "- Plain pain — [来源](https://x/3)",
    }
    report = analyzer.build_report(
        merged,
        5,
        "src(x)",
        "2026-09-16",
        "en",
        eval_cfg={"enabled": True, "keep_threshold": 16, "show_watch": True, "show_rejected": True},
    )
    assert "### Keep (1)" in report
    assert "### Watch (1)" in report
    assert "## Product Ideas" in report
    assert "## User Pain Points" in report


def test_analyze_eval_enabled_passes_to_multi_prompt(env, monkeypatch):
    """eval_enabled=True 时，analyze 给多类 prompt 注入评估规则；回退路径只对 ideas 注入。"""
    topics = [_post(1)]
    seen_prompts: list[str] = []

    def fake_call(prompt, **kw):
        seen_prompts.append(prompt)
        if "整理" in prompt:
            return "### A\n- [#1] x"
        # 多类输出解析失败 → 触发回退路径
        return "[#1] x"

    monkeypatch.setattr(analyzer, "call_api", fake_call)
    analyzer.analyze(topics, eval_enabled=True)

    multi_prompts = [p for p in seen_prompts if "一次调用提炼全部类别" in p or "按下面每个类别" in p]
    assert multi_prompts, "应至少有一次多类 prompt"
    assert any("[v=X,d=Y,✓/✗]" in p for p in multi_prompts), "多类 prompt 应含评分规则"

    # 回退路径里 ideas 分类的 prompt 含评分规则
    ideas_fallback = [p for p in seen_prompts if "只提炼「产品创意」" in p]
    assert ideas_fallback, "应触发回退到单类的产品创意 prompt"
    assert any("[v=X,d=Y,✓/✗]" in p for p in ideas_fallback)

    # 回退路径里其他分类不含评分规则
    pain_fallback = [p for p in seen_prompts if "只提炼「用户痛点」" in p]
    assert pain_fallback, "应触发回退到单类的用户痛点 prompt"
    assert not any("[v=X,d=Y,✓/✗]" in p for p in pain_fallback), "用户痛点不应含评分规则"
    indie_fallback = [p for p in seen_prompts if "只提炼「潜在机会」" in p]
    assert indie_fallback
    assert not any("[v=X,d=Y,✓/✗]" in p for p in indie_fallback), "潜在机会不应含评分规则"


# ---------- 前置过滤：watch_threshold / H3 三档解析 / token 累计 ----------


def test_load_evaluation_defaults_watch_threshold():
    """默认 watch_threshold=12。"""
    cfg = analyzer.load_evaluation({})
    assert cfg["keep_threshold"] == 16
    assert cfg["watch_threshold"] == 12
    assert cfg["show_watch"] is True
    assert cfg["show_rejected"] is True


def test_load_evaluation_watch_override():
    cfg = analyzer.load_evaluation({"evaluation": {"watch_threshold": 10}})
    assert cfg["watch_threshold"] == 10
    assert cfg["keep_threshold"] == 16  # 未指定沿用默认


def test_build_multi_prompt_embeds_thresholds_in_eval_rule():
    """eval_enabled=True 时，prompt 里含具体阈值（keep/watch）。"""
    cfg = {"enabled": True, "keep_threshold": 18, "watch_threshold": 10, "show_watch": True, "show_rejected": True}
    prompt = analyzer.build_multi_prompt("批次", idx=0, total=1, lang="zh", eval_enabled=True, eval_cfg=cfg)
    ideas_block = prompt[prompt.index("## 产品创意") : prompt.index("## 用户痛点")]
    assert "≥ 18" in ideas_block
    assert "[10, 18)" in ideas_block
    assert "< 10" in ideas_block


def test_build_batch_prompt_eval_enabled_uses_eval_cfg_thresholds():
    cfg = {"enabled": True, "keep_threshold": 20, "watch_threshold": 8}
    prompt = analyzer.build_batch_prompt(
        "batch", idx=0, total=1, title="产品创意", desc="def", lang="zh", eval_enabled=True, eval_cfg=cfg
    )
    assert "≥ 20" in prompt
    assert "[8, 20)" in prompt


def test_parse_eval_h3_sections_zh_basic():
    text = (
        "### 保留 (16+)\n"
        "- [v=4,d=2,✓] 高价值 — [#1]\n"
        "- [v=5,d=1,✓] 更高 — [#2]\n"
        "### 待观察 (12-15)\n"
        "- [v=3,d=3,✓] 中等 — [#3]\n"
        "### 被否决\n"
        "- [v=4,d=2,✗] 红线 — [#4]\n"
    )
    sections = analyzer.parse_eval_h3_sections(text, "zh")
    assert sections is not None
    assert len(sections["keep"]) == 2
    assert len(sections["watch"]) == 1
    assert len(sections["rejected"]) == 1
    assert "高价值" in sections["keep"][0]


def test_parse_eval_h3_sections_en_basic():
    text = (
        "### Keep (16+)\n"
        "- [v=4,d=2,✓] Hi — [#1]\n"
        "### Watch (12-15)\n"
        "- [v=3,d=3,✓] Med — [#2]\n"
        "### Rejected\n"
        "- [v=3,d=2,✗] No — [#3]\n"
    )
    sections = analyzer.parse_eval_h3_sections(text, "en")
    assert sections is not None
    assert len(sections["keep"]) == 1
    assert len(sections["watch"]) == 1
    assert len(sections["rejected"]) == 1


def test_parse_eval_h3_sections_no_match_returns_none():
    """没有 H3 → 返回 None（调用方应回退到评分字段解析）。"""
    text = "- [v=4,d=2,✓] 没分桶 — [#1]"
    assert analyzer.parse_eval_h3_sections(text, "zh") is None


def test_parse_eval_h3_sections_handles_threshold_suffix_variants():
    """兼容 (16+) / (12-15) / (≥16) 等阈值说明后缀的差异。"""
    text1 = "### 保留（16+）\n- a — [#1]\n"
    text2 = "### 保留\n- a — [#1]\n"
    assert analyzer.parse_eval_h3_sections(text1, "zh") is not None
    assert analyzer.parse_eval_h3_sections(text2, "zh") is not None


def test_categorize_items_h3_path_takes_priority():
    """H3 路径优先于评分字段路径——LLM 直接分桶时按 H3 走。"""
    text = (
        "### 保留 (16+)\n"
        "- [v=5,d=1,✓] 头等 — [#1]\n"
        "- [v=3,d=3,✓] 边界（12+） — [#2]\n"  # 总分=9 但 LLM 放进保留
        "### 待观察 (12-15)\n"
        "- [v=4,d=1,✓] 误放 — [#3]\n"  # 总分=20 但 LLM 放进待观察
    )
    buckets = analyzer.categorize_items(
        text, {"keep_threshold": 16, "watch_threshold": 12, "show_watch": True, "show_rejected": True}, "zh"
    )
    # 总分校正：#2(9) 应从 keep 移到 watch；#3(20) 应从 watch 移到 keep
    keep_ids = {line.split("[#")[1].rstrip("]") for _, _, _, line in buckets["keep"]}
    watch_ids = {line.split("[#")[1].rstrip("]") for _, _, _, line in buckets["watch"]}
    assert "1" in keep_ids
    assert "2" in watch_ids  # 总分 < 阈值 → 校正到 watch
    assert "3" in keep_ids  # 总分 ≥ 阈值 → 校正到 keep


def test_categorize_items_h3_path_no_h3_falls_back_to_score_field():
    """没 H3 → 回退到评分字段解析（旧行为）。"""
    text = "- [v=4,d=2,✓] 普通 — [#1]\n- 无评分 — [#2]"
    buckets = analyzer.categorize_items(text, {"keep_threshold": 16, "watch_threshold": 12}, "zh")
    assert len(buckets["keep"]) == 1
    assert len(buckets["unscored"]) == 1


def test_categorize_items_watch_threshold_filters_to_watch():
    """watch_threshold=10 时，总分 12..15 之间进 watch（与 keep_threshold=16 配合）。"""
    text = "- [v=3,d=3,✓] 中等 — [#1]"  # 总分=9
    buckets = analyzer.categorize_items(text, {"keep_threshold": 16, "watch_threshold": 10}, "zh")
    # H3 不存在 → 走评分字段路径
    assert len(buckets["keep"]) == 0
    assert len(buckets["watch"]) == 1
    assert len(buckets["unscored"]) == 0


def test_build_report_h3_text_renders_buckets():
    """LLM 直接按 H3 输出时，build_report 正确分桶呈现。"""
    merged = {
        "产品创意": (
            "### 保留 (16+)\n"
            "- [v=5,d=1,✓] 头条 — [来源](https://x/1)\n"
            "### 待观察 (12-15)\n"
            "- [v=3,d=3,✓] 中等 — [来源](https://x/2)\n"
            "### 被否决\n"
            "- [v=3,d=2,✗] 红线 — [来源](https://x/3)\n"
        ),
    }
    eval_cfg = {
        "enabled": True,
        "keep_threshold": 16,
        "watch_threshold": 12,
        "show_watch": True,
        "show_rejected": True,
    }
    report = analyzer.build_report(merged, 10, "src(x)", "2026-09-16", "zh", eval_cfg=eval_cfg)
    assert "### 保留清单 (1)" in report
    assert "### 待观察 (1)" in report
    assert "### 被否决 (1)" in report


def test_token_usage_tracks_across_calls(env, monkeypatch):
    """call_api 从服务端 usage 累计 token；服务未返回则不计入。"""
    analyzer.reset_token_usage()
    fake_call = lambda prompt, **kw: "ok"  # noqa: E731
    monkeypatch.setattr(analyzer, "call_api", fake_call)
    # 跳过真实 LLM 调用，直接验证 token 累加逻辑
    analyzer._TOKEN_USAGE["prompt"] += 100
    analyzer._TOKEN_USAGE["completion"] += 50
    analyzer._TOKEN_USAGE["total"] += 150
    analyzer._TOKEN_USAGE["calls"] += 1
    snap = analyzer.token_usage_snapshot()
    assert snap["prompt"] == 100
    assert snap["completion"] == 50
    assert snap["total"] == 150
    assert snap["calls"] == 1


def test_format_token_usage_no_usage():
    analyzer.reset_token_usage()
    assert "服务端未返回 usage" in analyzer.format_token_usage()


def test_format_token_usage_with_counts():
    analyzer.reset_token_usage()
    analyzer._TOKEN_USAGE.update({"prompt": 200, "completion": 100, "total": 300, "calls": 3})
    out = analyzer.format_token_usage()
    assert "prompt=200" in out
    assert "completion=100" in out
    assert "total=300" in out
    assert "3 次调用" in out


def test_call_api_accumulates_token_from_response(monkeypatch):
    """服务端 usage 正确累加到 _TOKEN_USAGE。"""
    analyzer.reset_token_usage()
    monkeypatch.setattr(analyzer, "_LLM", {"base_url": "http://fake/v1", "model": "m", "max_tokens": "4096"})

    response_body = json.dumps(
        {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 34, "total_tokens": 46},
        }
    ).encode()

    class FakeResp:
        def __init__(self, body):
            self._body = body

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, **kw):
        return FakeResp(response_body)

    monkeypatch.setattr(analyzer.urllib.request, "urlopen", fake_urlopen)
    text = analyzer.call_api("hi", retries=0)
    assert text == "ok"
    snap = analyzer.token_usage_snapshot()
    assert snap["prompt"] == 12
    assert snap["completion"] == 34
    assert snap["total"] == 46
    assert snap["calls"] == 1


def test_analyze_prints_token_summary(env, monkeypatch, capsys):
    """analyze 结束时打印 token 摘要（print_tokens=True 默认）。"""
    topics = [_post(1)]

    def fake_call(prompt, **kw):
        # 多类输出解析成功 → 不走回退
        return "## 产品创意\n- [#1] 创意\n## 用户痛点\n无\n## 潜在机会\n无"

    def fake_consolidate(results, category_title, incremental, lang):
        # 跳过真实 LLM 调用
        return "### 组\n- [#1] x" if results else ""

    monkeypatch.setattr(analyzer, "call_api", fake_call)
    monkeypatch.setattr(analyzer, "consolidate", fake_consolidate)

    analyzer.reset_token_usage()
    analyzer._TOKEN_USAGE.update({"prompt": 10, "completion": 5, "total": 15, "calls": 1})
    analyzer.analyze(topics, print_tokens=True)
    captured = capsys.readouterr()
    assert "token:" in captured.out


def test_analyze_no_token_print_when_disabled(env, monkeypatch, capsys):
    topics = [_post(1)]
    monkeypatch.setattr(analyzer, "call_api", lambda prompt, **kw: "[#1] x")
    monkeypatch.setattr(analyzer, "consolidate", lambda *a, **kw: "")
    analyzer.analyze(topics, print_tokens=False)
    captured = capsys.readouterr()
    assert "token:" not in captured.out
