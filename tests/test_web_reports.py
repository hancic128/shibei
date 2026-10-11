"""报告管理接口：DELETE /api/reports/<name> 与 POST /api/run 的 date 模式校验。"""

from web import app as web_app


def _make_app(monkeypatch, tmp_path):
    """隔离落盘：报告目录、任务目录、凭据/配置都指到 tmp_path。"""
    monkeypatch.setattr(web_app, "_ensure_secret_key", lambda: "test-secret-key")
    monkeypatch.setattr(web_app, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(web_app, "REPORT_DIR", tmp_path / "analysis")
    monkeypatch.setattr(web_app, "SECRETS_FILE", tmp_path / "web_secrets.json")
    monkeypatch.setattr(web_app, "WEB_CONFIG_FILE", tmp_path / "web_config.json")
    # 生产默认 base_path=/shibei/，会把 session cookie 限定在该前缀下；测试走根路径
    monkeypatch.setenv("SHIBEI_BASE_PATH", "/")
    return web_app.create_app()


def _login(client):
    with client.session_transaction() as s:
        s["user"] = "admin"


def test_delete_requires_login(monkeypatch, tmp_path):
    app = _make_app(monkeypatch, tmp_path)
    resp = app.test_client().delete("/api/reports/2026-08-03")
    assert resp.status_code == 401


def test_delete_removes_all_language_versions(monkeypatch, tmp_path):
    app = _make_app(monkeypatch, tmp_path)
    rd = web_app.REPORT_DIR
    rd.mkdir(parents=True)
    (rd / "2026-08-03.md").write_text("zh", encoding="utf-8")
    (rd / "2026-08-03.en.md").write_text("en", encoding="utf-8")

    client = app.test_client()
    _login(client)
    resp = client.delete("/api/reports/2026-08-03")

    assert resp.status_code == 200
    assert sorted(resp.get_json()["removed"]) == ["2026-08-03.en.md", "2026-08-03.md"]
    assert not (rd / "2026-08-03.md").exists()
    assert not (rd / "2026-08-03.en.md").exists()


def test_delete_rejects_bad_name(monkeypatch, tmp_path):
    app = _make_app(monkeypatch, tmp_path)
    client = app.test_client()
    _login(client)
    for bad in ("2026-02-30", "notadate", "analysis.bak"):
        assert client.delete(f"/api/reports/{bad}").status_code == 404


def test_delete_missing_file_returns_404(monkeypatch, tmp_path):
    app = _make_app(monkeypatch, tmp_path)
    web_app.REPORT_DIR.mkdir(parents=True, exist_ok=True)
    client = app.test_client()
    _login(client)
    assert client.delete("/api/reports/2099-01-01").status_code == 404


def test_run_date_mode_validation(monkeypatch, tmp_path):
    app = _make_app(monkeypatch, tmp_path)
    client = app.test_client()
    _login(client)

    missing = client.post("/api/run", json={"mode": "date", "lang": "zh"})
    assert missing.status_code == 400
    assert missing.get_json()["error"] == "bad_date"

    invalid = client.post("/api/run", json={"mode": "date", "date": "2026-02-30", "lang": "zh"})
    assert invalid.status_code == 400
    assert invalid.get_json()["error"] == "bad_date"

    unknown = client.post("/api/run", json={"mode": "nope"})
    assert unknown.status_code == 400
    assert unknown.get_json()["error"] == "bad_mode"
