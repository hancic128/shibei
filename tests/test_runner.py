"""任务运行器：analyzer 子进程的 argv 组装与任务记录（mode=date 历史重算）。"""

import pytest

from web import runner


class _FakeProc:
    """替身 Popen：记录调用参数，wait() 立即成功返回。"""

    def __init__(self) -> None:
        pass

    def wait(self) -> int:
        return 0

    def terminate(self) -> None:  # pragma: no cover - 仅接口占位
        pass


def _capture(monkeypatch) -> dict:
    captured: dict = {}

    def fake_popen(argv, **kw):
        captured["argv"] = argv
        captured["kw"] = kw
        return _FakeProc()

    monkeypatch.setattr(runner.subprocess, "Popen", fake_popen)
    return captured


def test_start_date_mode_passes_date_flag(monkeypatch, tmp_path):
    captured = _capture(monkeypatch)
    tm = runner.TaskManager(tmp_path)
    task = tm.start("date", {"ANALYZE_LANG": "zh"}, date="2026-08-03")

    assert task["mode"] == "date"
    assert task["date"] == "2026-08-03"
    assert captured["argv"][-2:] == ["--date", "2026-08-03"]


def test_start_full_mode_passes_full_flag(monkeypatch, tmp_path):
    captured = _capture(monkeypatch)
    task = runner.TaskManager(tmp_path).start("full", {"ANALYZE_LANG": "zh"})
    assert task["date"] is None
    assert captured["argv"][-1] == "--full"


def test_start_today_mode_has_no_extra_flag(monkeypatch, tmp_path):
    captured = _capture(monkeypatch)
    task = runner.TaskManager(tmp_path).start("today", {"ANALYZE_LANG": "zh"})
    assert task["date"] is None
    assert captured["argv"][-1].endswith("analyzer.py")  # 无额外标志


def test_start_date_mode_requires_date(tmp_path):
    tm = runner.TaskManager(tmp_path)
    with pytest.raises(ValueError):
        tm.start("date", {}, date=None)


def test_start_unknown_mode_rejected(tmp_path):
    tm = runner.TaskManager(tmp_path)
    with pytest.raises(ValueError):
        tm.start("nope", {})
