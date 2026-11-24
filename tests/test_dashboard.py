"""Panel en vivo: servidor HTTP local, solo lectura salvo el botón de parada."""

import json
import threading
import urllib.error
import urllib.request

import pytest

from mt5_mcp import dashboard
from mt5_mcp.broker import MT5Broker
from mt5_mcp.config import AutoConfig, RiskConfig
from mt5_mcp.engine import Engine
from mt5_mcp.journal import Journal


@pytest.fixture
def panel(fake, tmp_path):
    cfg = RiskConfig(symbols=("EURUSD",), max_volume_per_order=1.0, max_total_volume=3.0, auto=AutoConfig(enabled=True))
    engine = Engine(MT5Broker(module=fake), cfg=cfg, root=tmp_path, journal=Journal(tmp_path / "journal" / "trades.jsonl"))
    monitor = dashboard.Monitor(engine)
    httpd = dashboard.serve(monitor, 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    yield {"url": f"http://127.0.0.1:{port}", "port": port, "monitor": monitor, "engine": engine, "fake": fake, "root": tmp_path}
    httpd.shutdown()
    httpd.server_close()


def call(url, method="GET", headers=None):
    req = urllib.request.Request(url, method=method, headers=headers or {}, data=b"" if method == "POST" else None)
    try:
        with urllib.request.urlopen(req, timeout=5) as res:
            return res.status, res.read().decode("utf-8"), res.headers
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8"), exc.headers


def test_page_and_state(panel):
    code, html, headers = call(panel["url"] + "/")
    assert code == 200 and "Panel en vivo" in html and panel["monitor"].token in html and "__PANEL_TOKEN__" not in html
    assert headers["X-Frame-Options"] == "DENY" and "default-src 'self'" in headers["Content-Security-Policy"]
    assert "innerHTML" not in html  # los datos del journal se pintan siempre como texto
    code, body, _ = call(panel["url"] + "/api/state")
    state = json.loads(body)
    assert code == 200 and state["ok"] and state["demo"] is False
    assert state["account"]["mode"] == "DEMO" and state["auto"]["enabled"] and state["risk"]["open_positions"] == 0
    assert state["equity_series"][-1][1] == 10_000.0 and state["positions"] == []


def test_state_reports_terminal_errors_without_crashing(panel):
    panel["fake"].fail_positions = True
    panel["monitor"]._cached = None
    code, body, _ = call(panel["url"] + "/api/state")
    state = json.loads(body)
    assert code == 200 and state["ok"] is False and "positions_get" in state["error"]
    panel["fake"].fail_positions = False
    panel["monitor"]._cached = None
    assert json.loads(call(panel["url"] + "/api/state")[1])["ok"] is True


def test_reading_the_panel_never_writes(panel):
    for _ in range(3):
        panel["monitor"]._cached = None
        call(panel["url"] + "/api/state")
    assert not panel["fake"].sent and not panel["fake"].checked
    assert panel["engine"].journal_tail() == [] and not (panel["root"] / "HALT").exists()


def test_halt_button_needs_token_and_only_blocks(panel):
    panel["fake"].add_position("EURUSD", "buy", 0.1, 1.0990, sl=1.0970)
    assert call(panel["url"] + "/api/halt", "POST")[0] == 403
    assert call(panel["url"] + "/api/halt", "POST", {"X-Panel-Token": "malo"})[0] == 403
    assert call(panel["url"] + "/api/halt", "GET")[0] == 404
    assert not (panel["root"] / "HALT").exists()
    code, body, _ = call(panel["url"] + "/api/halt", "POST", {"X-Panel-Token": panel["monitor"].token})
    assert code == 200 and json.loads(body)["halted"] is True and (panel["root"] / "HALT").exists()
    assert not panel["fake"].sent and len(panel["engine"].positions()) == 1  # no cierra posiciones
    state = json.loads(call(panel["url"] + "/api/state")[1])
    assert state["risk"]["halted"] is True and state["auto"]["can_open"] is False


def test_rejects_foreign_host_and_unknown_paths(panel):
    assert call(panel["url"] + "/api/state", headers={"Host": "evil.example"})[0] == 403
    assert call(panel["url"] + "/", headers={"Host": f"evil.example:{panel['port']}"})[0] == 403
    assert call(panel["url"] + "/api/halt", "POST", {"Host": "evil.example", "X-Panel-Token": panel["monitor"].token})[0] == 403
    assert call(panel["url"] + "/risk.toml")[0] == 404 and call(panel["url"] + "/../risk.toml")[0] == 404
    assert call(f"http://localhost:{panel['port']}/api/state")[0] == 200


def test_server_binds_to_loopback_only(panel):
    assert panel["monitor"] and dashboard.serve.__doc__ is None
    httpd = dashboard.serve(panel["monitor"], 0)
    try:
        assert httpd.server_address[0] == "127.0.0.1"
    finally:
        httpd.server_close()


def test_demo_mode_builds_simulated_state():
    engine, on_poll = dashboard._demo_engine()
    monitor = dashboard.Monitor(engine, demo=True, on_poll=on_poll)
    state = monitor.state()
    assert state["ok"] and state["demo"] and len(state["positions"]) == 2 and state["auto"]["enabled"]
    assert {p["opened_by"] for p in state["positions"]} == {"auto", "manual"}
