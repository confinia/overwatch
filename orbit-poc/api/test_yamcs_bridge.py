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
