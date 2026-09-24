"""The YAMCS bridge (#423) against a mocked YAMCS and a mocked Overwatch.

No live services: one local HTTP server plays both roles (the batchGet
endpoint and the tenant telemetry endpoint), which exercises the bridge's
real transport path, not monkeypatched internals.
"""

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge" / "yamcs"))
import bridge  # noqa: E402
import core    # noqa: E402


# --- unit: value flattening -------------------------------------------------

@pytest.mark.parametrize("eng,expected", [
    ({"type": "FLOAT", "floatValue": 12.5}, 12.5),
    ({"type": "DOUBLE", "doubleValue": -3.25}, -3.25),
    ({"type": "SINT32", "sint32Value": -7}, -7.0),
    ({"type": "UINT64", "uint64Value": 42}, 42.0),
    ({"type": "BOOLEAN", "booleanValue": True}, 1.0),
    ({"type": "BOOLEAN", "booleanValue": False}, 0.0),
    ({"type": "STRING", "stringValue": "SAFE"}, "SAFE"),
    ({"type": "ENUMERATED", "enumValue": "ON"}, "ON"),
])
def test_scalar_flattens_the_value_union(eng, expected):
    assert bridge.scalar(eng) == expected


def test_field_name_is_basename_unless_mapped():
    assert bridge.field_name("/YSS/SIMULATOR/Alpha", {}) == "Alpha"
    assert bridge.field_name("/YSS/SIMULATOR/Alpha",
                             {"/YSS/SIMULATOR/Alpha": "alpha_deg"}) == "alpha_deg"


def test_config_rejects_missing_env_and_bad_field_map():
    with pytest.raises(SystemExit):
        bridge.load_config(env={"YAMCS_URL": "http://x"})
    good = {"YAMCS_URL": "http://x", "YAMCS_INSTANCE": "sim",
            "YAMCS_PARAMETERS": "/A/B", "OVERWATCH_URL": "http://y",
            "TENANT_KEY": "k", "SATELLITE": "S"}
    with pytest.raises(SystemExit):
        bridge.load_config(env={**good, "YAMCS_FIELD_MAP": "no-equals-sign"})
    cfg = bridge.load_config(env={**good, "YAMCS_FIELD_MAP": "/A/B=b"})
    assert cfg.processor == "realtime" and cfg.field_map == {"/A/B": "b"}


# --- integration: one server, both seams ------------------------------------

class Fake(BaseHTTPRequestHandler):
    # class-level state, reset per test via fake_server
    batch_values: list = []
    pushes: list = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path.endswith("parameters:batchGet"):
            payload, code = {"value": type(self).batch_values}, 200
        elif "/v1/tenants/" in self.path and self.path.endswith("/telemetry"):
            type(self).pushes.append(body)
            payload, code = {"accepted": len(body["points"])}, 202
        else:
            payload, code = {"error": "unexpected " + self.path}, 404
        out = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):        # keep pytest output clean
        pass


@pytest.fixture
def fake_server():
    Fake.batch_values, Fake.pushes = [], []
    srv = HTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def _cfg(base):
    return bridge.Config(
        yamcs_url=base, instance="sim", processor="realtime",
        parameters=["/YSS/SIMULATOR/BatteryVoltage1", "/YSS/SIMULATOR/Mode"],
        field_map={}, overwatch_url=base, tenant_key="tkey", satellite="SIM")


def _pv(name, gen, eng):
    return {"id": {"name": name}, "generationTime": gen, "engValue": eng}


def test_bridge_pushes_new_samples_and_dedupes(fake_server):
    cfg, state = _cfg(fake_server), bridge.State()
    Fake.batch_values = [
        _pv("/YSS/SIMULATOR/BatteryVoltage1", "2026-09-02T10:00:00.123Z",
            {"type": "FLOAT", "floatValue": 12.1}),
        _pv("/YSS/SIMULATOR/Mode", "2026-09-02T10:00:00.123Z",
            {"type": "ENUMERATED", "enumValue": "SAFE"}),
    ]
    assert bridge.run_once(cfg, state) == 2
    assert bridge.run_once(cfg, state) == 0          # same generationTime: no re-push

    Fake.batch_values[0] = _pv("/YSS/SIMULATOR/BatteryVoltage1",
                               "2026-09-02T10:00:10.123Z",
                               {"type": "FLOAT", "floatValue": 12.0})
    assert bridge.run_once(cfg, state) == 1          # only the newer sample

    assert [p["field"] for p in Fake.pushes[0]["points"]] == \
        ["BatteryVoltage1", "Mode"]
    assert Fake.pushes[0]["satellite"] == "SIM"
    assert Fake.pushes[0]["points"][1]["value"] == "SAFE"
    assert Fake.pushes[1]["points"][0]["value"] == 12.0


def test_bridge_skips_malformed_values_without_dying(fake_server):
    cfg, state = _cfg(fake_server), bridge.State()
    Fake.batch_values = [
        {"id": {"name": "/YSS/SIMULATOR/Mode"}},                # no time, no value
        _pv("/YSS/SIMULATOR/BatteryVoltage1",
            "2026-09-02T10:00:00Z", {"type": "FLOAT", "floatValue": 11.9}),
    ]
    assert bridge.run_once(cfg, state) == 1


# --- the adapter seam (#425): the core is MCS-neutral -----------------------

def test_core_contract_needs_no_yamcs_shapes():
    """A SCOS-style adapter: bare Samples in, dedupe and naming out —
    no ParameterValue dicts, no value unions, nothing YAMCS anywhere."""
    st = core.State()
    samples = [core.Sample("NPWD2401", "2026-09-02T10:00:00Z", 42.0),
               core.Sample("NPWD2401", "2026-09-02T10:00:05Z", 43.0)]
    pts = core.to_points(samples, {"NPWD2401": "power_w"}, st)
    assert [p["value"] for p in pts] == [42.0, 43.0]
    assert {p["field"] for p in pts} == {"power_w"}
    # a file re-read or cache resend: the dedupe absorbs the tail sample
    assert core.to_points(samples[1:], {}, st) == []
    # a slashless mnemonic keeps itself as the default field name
    assert core.field_name("NPWD2401", {}) == "NPWD2401"


# --- WebSocket subscription (#424): the protocol pieces, no live socket -----

def test_ws_endpoint_maps_the_scheme():
    cfg = _cfg("http://yamcs:8090")
    assert bridge.ws_endpoint(cfg) == "ws://yamcs:8090/api/websocket"
    cfg = _cfg("https://mcs.example.eu")
    assert bridge.ws_endpoint(cfg) == "wss://mcs.example.eu/api/websocket"


def test_subscribe_msg_shape():
    msg = bridge.subscribe_msg(_cfg("http://y:8090"))
    assert msg["type"] == "parameters"
    assert msg["options"]["instance"] == "sim"
    assert msg["options"]["processor"] == "realtime"
    assert msg["options"]["id"][0] == {"name": "/YSS/SIMULATOR/BatteryVoltage1"}
    # one renamed parameter must not kill the whole subscription
    assert msg["options"]["abortOnInvalid"] is False
    assert msg["options"]["sendFromCache"] is True


def test_ws_extract_resolves_numeric_id_indirection():
    mapping = {}
    # first data message: mapping + values still carrying full ids
    first = {"mapping": {"7": {"name": "/YSS/SIMULATOR/BatteryVoltage1"}},
             "values": [_pv("/YSS/SIMULATOR/BatteryVoltage1",
                            "2026-09-02T10:00:00Z",
                            {"type": "FLOAT", "floatValue": 12.1})]}
    assert [v["id"]["name"] for v in bridge.ws_extract(first, mapping)] == \
        ["/YSS/SIMULATOR/BatteryVoltage1"]
    # later messages: numericId only — the accumulated mapping must resolve it
    later = {"values": [{"numericId": 7,
                         "generationTime": "2026-09-02T10:00:10Z",
                         "engValue": {"type": "FLOAT", "floatValue": 12.0}}]}
    out = bridge.ws_extract(later, mapping)
    assert out[0]["id"]["name"] == "/YSS/SIMULATOR/BatteryVoltage1"
    # unknown numericId: skipped, not crashed
    orphan = {"values": [{"numericId": 99, "generationTime": "x",
                          "engValue": {"type": "FLOAT", "floatValue": 1}}]}
    assert bridge.ws_extract(orphan, mapping) == []


def test_ws_extract_feeds_to_points_with_dedupe():
    cfg, state, mapping = _cfg("http://y"), bridge.State(), {}
    data = {"mapping": {"1": {"name": "/YSS/SIMULATOR/Alpha"}},
            "values": [{"numericId": 1,
                        "generationTime": "2026-09-02T10:00:00Z",
                        "engValue": {"type": "FLOAT", "floatValue": 3.5}}]}
    pts = bridge.to_points(bridge.ws_extract(data, mapping), cfg, state)
    assert pts == [{"ts": "2026-09-02T10:00:00Z", "field": "Alpha",
                    "value": 3.5}]
    # the cache resend after a reconnect is absorbed by the dedupe
    assert bridge.to_points(bridge.ws_extract(data, {}), cfg, state) == []


def test_mode_config():
    good = {"YAMCS_URL": "http://x", "YAMCS_INSTANCE": "sim",
            "YAMCS_PARAMETERS": "/A/B", "OVERWATCH_URL": "http://y",
            "TENANT_KEY": "k", "SATELLITE": "S"}
    assert bridge.load_config(env=good).mode == "auto"
    assert bridge.load_config(env={**good, "YAMCS_MODE": "WS"}).mode == "ws"
    with pytest.raises(SystemExit):
        bridge.load_config(env={**good, "YAMCS_MODE": "carrier-pigeon"})


def test_push_chunks_at_the_api_limit(fake_server):
    cfg = _cfg(fake_server)
    points = [{"ts": "2026-09-02T10:00:00Z", "field": f"f{i}", "value": i}
              for i in range(2500)]
    assert bridge.push(cfg, points) == 2500
    assert [len(p["points"]) for p in Fake.pushes] == [1000, 1000, 500]


# --- the live proof and what it surfaced (#428) -----------------------------

DEMO = Path(__file__).resolve().parents[1] / "bridge" / "yamcs" / "demo"


def test_the_bridge_exits_on_sigterm_as_pid_1():
    """`docker stop` waited 10 s and SIGKILLed the live bridge every time:
    PID 1 has no default SIGTERM disposition. A handler is the fix."""
    import inspect
    import signal
    src = inspect.getsource(bridge.main)
    assert "signal.SIGTERM" in src and "sys.exit(0)" in src
    assert signal.getsignal(signal.SIGTERM) is not None


def _drive_auto_until_poll_or(monkeypatch, cycles, yamcs_up):
    """Run main() in auto mode with a subscription that never establishes;
    returns the log lines after `cycles` sleeps (the loop is escaped by
    making the sleep raise on the last one)."""
    out = []
    env = {"YAMCS_URL": "http://y", "YAMCS_INSTANCE": "i", "TENANT_KEY": "k",
           "SATELLITE": "S", "YAMCS_PARAMETERS": "/a", "OVERWATCH_URL": "http://o",
           "YAMCS_MODE": "auto", "POLL_SECONDS": "1"}
    real_load = bridge.load_config
    monkeypatch.setattr(bridge, "load_config", lambda *a, **k: real_load(env))
    monkeypatch.setattr(bridge.signal, "signal", lambda *a: None)
    monkeypatch.setattr(bridge, "run_ws", lambda *a: (_ for _ in ()).throw(
        ConnectionRefusedError(111, "Connection refused")))
    monkeypatch.setattr(bridge, "yamcs_answers", lambda cfg: yamcs_up)
    monkeypatch.setattr(bridge, "run_once", lambda *a: 0)
    n = {"sleeps": 0}

    def sleep(_):
        n["sleeps"] += 1
        if n["sleeps"] >= cycles:
            raise KeyboardInterrupt
    monkeypatch.setattr(bridge.time, "sleep", sleep)
    monkeypatch.setattr(bridge, "print", lambda *a, **k: out.append(" ".join(map(str, a))),
                        raising=False)
    with pytest.raises(KeyboardInterrupt):
        bridge.main()
    return out


def test_auto_keeps_trying_the_subscription_while_yamcs_boots(monkeypatch):
    """The live proof: compose starts the bridge seconds into YAMCS's boot,
    the first ws attempt is refused, and the old rule ("never established ->
    poll") downgraded the demo to polling for its whole life. A YAMCS that
    does not answer polls either is booting, not refusing WebSockets."""
    out = _drive_auto_until_poll_or(monkeypatch, cycles=3, yamcs_up=False)
    assert not any("falling back to polling" in line for line in out), out
    assert sum("ws subscription failed" in line for line in out) == 3


def test_auto_falls_back_when_yamcs_answers_polls_but_not_the_ws(monkeypatch):
    out = _drive_auto_until_poll_or(monkeypatch, cycles=2, yamcs_up=True)
    assert any("falling back to polling" in line for line in out), out


def test_yamcs_answers_is_the_poll_endpoint(monkeypatch):
    """"Fall back" must mean "polling is proven to work right now"."""
    cfg = bridge.load_config({"YAMCS_URL": "http://y", "YAMCS_INSTANCE": "i",
                              "TENANT_KEY": "k", "SATELLITE": "S",
                              "YAMCS_PARAMETERS": "/a", "OVERWATCH_URL": "http://o"})
    monkeypatch.setattr(bridge, "fetch", lambda c: [])
    assert bridge.yamcs_answers(cfg) is True
    monkeypatch.setattr(bridge, "fetch", lambda c: (_ for _ in ()).throw(OSError("down")))
    assert bridge.yamcs_answers(cfg) is False


def test_the_demo_simulator_is_restarted_before_its_data_runs_out():
    """The quickstart's simulator.py plays testdata.ccsds ONCE (86,400 packets
    = 24 h at 1 Hz) then idles alive: the live demo went silent for 16 days
    while every container reported healthy."""
    sh = (DEMO / "start.sh").read_text()
    assert "while true; do" in sh
    assert "timeout 86000 python3 simulator.py" in sh, "restart before the 86,400th packet"


def test_the_demo_mode_is_env_driven_for_the_proof():
    yml = (DEMO / "docker-compose.yml").read_text()
    assert "YAMCS_MODE: ${YAMCS_MODE:-auto}" in yml


def test_the_live_proof_asserts_each_deliverable():
    """#428's deliverables, each an assertion in prove.sh: points within N s,
    ws mode in the logs, a reconnect after killing the socket (YAMCS restart)
    without a downgrade, no duplicate stamps, the poll fallback."""
    sh = (DEMO / "prove.sh").read_text()
    for needle in (
        "/v1/tenants/$TENANT_KEY/satellites",
        'yamcs-bridge \\[auto\\]\\|yamcs-bridge \\[ws\\]',
        "compose restart yamcs",
        "ws session dropped, reconnecting",
        '"falling back to polling" && fail',
        "len(ts) == len(set(ts))",
        "YAMCS_MODE=poll compose up -d --force-recreate bridge",
        'wait_log 60 "yamcs-bridge \\[poll\\]"',
    ):
        assert needle in sh, needle
    import os
    assert os.access(DEMO / "prove.sh", os.X_OK)


def test_the_internal_overlay_joins_the_stack_network():
    yml = (DEMO / "docker-compose.internal.yml").read_text()
    assert "external: true" in yml
    assert "OVERWATCH_URL: ${OVERWATCH_URL:-http://api:8000}" in yml
