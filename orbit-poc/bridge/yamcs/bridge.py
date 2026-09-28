"""YAMCS -> Overwatch bridge (#423).

A standalone poller: reads parameter values from a YAMCS processor over the
HTTP API (`parameters:batchGet`) and pushes them into an Overwatch tenant
through `POST /v1/tenants/{key}/telemetry`. The MCS is not modified; the
Overwatch API is not modified. The bridge is only the pipe between two
seams that already exist.

Everything MCS-neutral (dedupe, field naming, the chunked tenant push)
lives one directory up in core.py (#425); this file is only what is
YAMCS-shaped — the value-union flattening and the two pull modes.

Two pull modes (#424). The WebSocket subscription delivers every update as
it happens; polling REST is version-tolerant and debuggable with curl.
YAMCS_MODE=auto (the default) tries the subscription and falls back to
polling only if it never establishes while YAMCS does answer polls, so
awkward networks still work and a booting YAMCS is waited for.

Config is environment-only so `docker compose up -d` is the whole install:

    YAMCS_URL         http://yamcs:8090            (required)
    YAMCS_INSTANCE    simulator                    (required)
    YAMCS_PROCESSOR   realtime                     (default: realtime)
    YAMCS_PARAMETERS  /YSS/SIMULATOR/BatteryVoltage1,/YSS/SIMULATOR/Alpha
                                                   (required, comma-separated
                                                    qualified names)
    YAMCS_FIELD_MAP   /YSS/SIMULATOR/Alpha=alpha_deg
                                                   (optional, comma-separated
                                                    qname=field overrides)
    OVERWATCH_URL     https://overwatch.confinia.io (required)
    TENANT_KEY        <your tenant key>            (required)
    SATELLITE         MYSAT                        (required)
    POLL_SECONDS      10                           (default: 10; also the
                                                    reconnect pause in ws mode)
    YAMCS_MODE        auto | ws | poll             (default: auto)
    OVERWATCH_HOST    overwatch.confinia.io        (optional: Host header for
                                                    OVERWATCH_URL, needed when
                                                    that points at a caddy
                                                    rather than an api - #524)
    LOG_EVERY_S       60                           (default: 60; how often to
                                                    report pushes. At 1 Hz a
                                                    line per push is a line
                                                    per second - #511)
"""

import json
import os
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import requests

try:
    # flat layout in the container (/app/core.py next to /app/bridge.py)
    from core import (Sample, State, field_name,          # noqa: F401
                      to_points as core_points, push as core_push)
except ImportError:
    # repo layout: core.py lives one directory up
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from core import (Sample, State, field_name,          # noqa: F401
                      to_points as core_points, push as core_push)


@dataclass
class Config:
    yamcs_url: str
    instance: str
    processor: str
    parameters: list[str]
    field_map: dict[str, str]
    overwatch_url: str
    tenant_key: str
    satellite: str
    poll_seconds: float = 10.0
    mode: str = "auto"
    # Host header for OVERWATCH_URL, when that URL points at a caddy rather
    # than straight at an api container (#524). Empty = send none.
    overwatch_host: str = ""


def load_config(env=os.environ) -> Config:
    missing = [k for k in ("YAMCS_URL", "YAMCS_INSTANCE", "YAMCS_PARAMETERS",
                           "OVERWATCH_URL", "TENANT_KEY", "SATELLITE")
               if not env.get(k)]
    if missing:
        raise SystemExit(f"missing required env: {', '.join(missing)}")
    fmap = {}
    for pair in filter(None, (p.strip() for p in
                              env.get("YAMCS_FIELD_MAP", "").split(","))):
        qname, _, name = pair.partition("=")
        if not name:
            raise SystemExit(f"YAMCS_FIELD_MAP entry without '=': {pair!r}")
        fmap[qname.strip()] = name.strip()
    mode = env.get("YAMCS_MODE", "auto").strip().lower()
    if mode not in ("auto", "ws", "poll"):
        raise SystemExit(f"YAMCS_MODE must be auto, ws or poll, not {mode!r}")
    return Config(
        yamcs_url=env["YAMCS_URL"].rstrip("/"),
        instance=env["YAMCS_INSTANCE"],
        processor=env.get("YAMCS_PROCESSOR", "realtime"),
        parameters=[p.strip() for p in env["YAMCS_PARAMETERS"].split(",")
                    if p.strip()],
        field_map=fmap,
        overwatch_url=env["OVERWATCH_URL"].rstrip("/"),
        tenant_key=env["TENANT_KEY"],
        satellite=env["SATELLITE"],
        poll_seconds=float(env.get("POLL_SECONDS", "10")),
        mode=mode,
        overwatch_host=env.get("OVERWATCH_HOST", "").strip(),
    )


def scalar(eng_value: dict):
    """Flatten a YAMCS Value union to the tenant API's float-or-string.
    engValue carries exactly one <type>Value member next to 'type'."""
    for key, val in eng_value.items():
        if key == "type":
            continue
        if key in ("floatValue", "doubleValue", "sint32Value", "uint32Value",
                   "sint64Value", "uint64Value"):
            return float(val)
        if key == "booleanValue":
            return 1.0 if val else 0.0
        return str(val)           # stringValue, enumValue, binaryValue, ...
    return None


def yamcs_samples(values: list[dict]) -> list[Sample]:
    """ParameterValue dicts -> the neutral Sample the core consumes.
    This function IS the YAMCS adapter, in the #425 contract sense."""
    out = []
    for pv in values:
        qname = (pv.get("id") or {}).get("name")
        gen = pv.get("generationTime")
        eng = pv.get("engValue") or pv.get("rawValue")
        if not qname or not gen or eng is None:
            continue
        value = scalar(eng)
        if value is None:
            continue
        out.append(Sample(qname, gen, value))
    return out


def to_points(values: list[dict], cfg: Config, state: State) -> list[dict]:
    return core_points(yamcs_samples(values), cfg.field_map, state)


def push(cfg: Config, points: list[dict]) -> int:
    headers = {"Host": cfg.overwatch_host} if cfg.overwatch_host else None
    return core_push(cfg.overwatch_url, cfg.tenant_key, cfg.satellite, points,
                     headers=headers)


def fetch(cfg: Config) -> list[dict]:
    """One batchGet against the processor: what an operator's display sees."""
    url = (f"{cfg.yamcs_url}/api/processors/{cfg.instance}/{cfg.processor}"
           "/parameters:batchGet")
    body = {"id": [{"name": q} for q in cfg.parameters]}
    r = requests.post(url, json=body, timeout=15)
    r.raise_for_status()
    data = r.json()
    return data.get("value") or data.get("values") or []


def yamcs_answers(cfg: Config) -> bool:
    """Would polling work right now? Decides whether a failed subscription
    means "this YAMCS has no WebSocket for us" (fall back to polling) or
    "YAMCS is not up yet" (keep trying the subscription). The live proof
    caught the difference: compose starts the bridge seconds into a
    one-minute YAMCS boot, and a fallback there is a permanent, silent
    downgrade to polling resolution (#428)."""
    try:
        fetch(cfg)
        return True
    except Exception:
        return False


def run_once(cfg: Config, state: State) -> int:
    """One poll cycle. Returns points pushed. Raises on transport errors;
    the loop catches, the tests call it directly."""
    points = to_points(fetch(cfg), cfg, state)
    return push(cfg, points) if points else 0


# --- WebSocket subscription (#424) -----------------------------------------

def ws_endpoint(cfg: Config) -> str:
    scheme = "wss" if cfg.yamcs_url.startswith("https") else "ws"
    return f"{scheme}://{cfg.yamcs_url.split('://', 1)[1]}/api/websocket"


def subscribe_msg(cfg: Config) -> dict:
    """The 'parameters' topic subscription. abortOnInvalid=False on purpose:
    one renamed parameter must not kill the whole subscription (the server
    reports the misses in 'invalid' and we log them instead)."""
    return {"type": "parameters", "id": 1, "options": {
        "instance": cfg.instance, "processor": cfg.processor,
        "id": [{"name": q} for q in cfg.parameters],
        "sendFromCache": True, "abortOnInvalid": False}}


def ws_extract(data: dict, mapping: dict) -> list[dict]:
    """Values as `to_points` expects them. After the first message YAMCS
    switches to numeric-id indirection (a one-time 'mapping', then values
    carrying only numericId), so resolve through the accumulated mapping —
    reading id.name alone would go silent after the first frame."""
    for num, named in (data.get("mapping") or {}).items():
        mapping[str(num)] = named.get("name")
    out = []
    for pv in data.get("values") or []:
        if not (pv.get("id") or {}).get("name"):
            name = mapping.get(str(pv.get("numericId")))
            if not name:
                continue
            pv = {**pv, "id": {"name": name}}
        out.append(pv)
    return out


class Link:
    """Has a subscription EVER delivered a parameter, across reconnects?

    run_ws cannot report this by returning. When something raises out of an
    established session the local `established` dies with the frame, and the
    caller cannot tell a ten-minute-old subscription that dropped from one
    that never connected — so it downgraded the public demo to polling and
    left it there (#521). Process-lifetime, deliberately: once the WebSocket
    has been proven to work, a later failure is a reconnect, never a reason
    to accept a tenth of the resolution for good.
    """

    def __init__(self):
        self.established = False


def run_ws(cfg: Config, state: State, progress: "Progress",
           link: "Link") -> None:
    """One WebSocket session: subscribe, push until the socket drops.
    Returns when an ESTABLISHED session drops (caller reconnects); raises
    when the subscription cannot be established (caller may fall back)."""
    import websocket           # lazy: poll mode must not require the lib
    conn = websocket.create_connection(ws_endpoint(cfg), timeout=30)
    established = False
    try:
        conn.send(json.dumps(subscribe_msg(cfg)))
        # A quiet-but-alive link reconnects every 5 min; sendFromCache then
        # resends latest values and the generationTime dedupe absorbs them.
        conn.settimeout(300)
        mapping: dict = {}
        while True:
            try:
                raw = conn.recv()
            except Exception as exc:
                if established:
                    print(f"ws session dropped, reconnecting: {exc}",
                          file=sys.stderr, flush=True)
                    return
                raise
            msg = json.loads(raw)
            data = msg.get("data") or {}
            if msg.get("type") == "reply":
                if data.get("exception"):
                    raise RuntimeError(f"subscription refused: "
                                       f"{data['exception']}")
                established = link.established = True
                continue
            if msg.get("type") != "parameters":
                continue
            established = link.established = True
            if data.get("invalid"):
                print(f"parameters YAMCS does not know: {data['invalid']}",
                      file=sys.stderr, flush=True)
            points = to_points(ws_extract(data, mapping), cfg, state)
            if points:
                # A push failure is news about OVERWATCH, not about the
                # subscription (#521). Letting it out of here threw away a
                # healthy YAMCS session because the api container was being
                # recreated mid-deploy — `('Connection aborted.',
                # ConnectionResetError(104))`, the requests shape, not a
                # WebSocket one. Keep listening; the next push carries on.
                try:
                    progress.add(push(cfg, points))
                except Exception as exc:
                    print(f"push failed, subscription kept: {exc}",
                          file=sys.stderr, flush=True)
    finally:
        try:
            conn.close()
        except Exception:
            pass


class Progress:
    """Rolling push counter that reports on a timer, not on every push (#511).

    One line per push is one line per second at 1 Hz, and the demo bridge alone
    was 14% of the whole VM's log volume saying `pushed 8 points` over and
    over. The useful signal is not each push, it is that pushes are still
    happening and at what rate, which a periodic line carries just as well and
    ~60x more cheaply.

    The first push still reports immediately: waiting a minute to find out
    whether a fresh bridge works at all is a worse trade than the lines saved.
    """

    def __init__(self, every_s: float, now=time.monotonic, out=None):
        self.every_s = every_s
        self._now = now
        self._out = out or (lambda m: print(m, flush=True))
        self.points = 0
        self.batches = 0
        self._last = None          # None = nothing reported yet

    def add(self, n: int) -> None:
        self.points += n
        self.batches += 1
        t = self._now()
        if self._last is None:                      # the first push, at once
            self._out(f"pushed {n} points")
            self._last = t
            self.points = self.batches = 0
            return
        if t - self._last >= self.every_s:
            secs = t - self._last
            self._out(f"pushed {self.points} points in {self.batches} batches "
                      f"over {secs:.0f}s")
            self._last = t
            self.points = self.batches = 0


def main() -> None:
    # PID 1 in a container gets no default signal disposition: without a
    # handler, `docker stop` waits its grace period and SIGKILLs the bridge
    # every time (seen on the live run, #428). Exit cleanly instead.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    cfg = load_config()
    state = State()
    progress = Progress(float(os.environ.get("LOG_EVERY_S", "60")))
    print(f"yamcs-bridge [{cfg.mode}]: {len(cfg.parameters)} parameters from "
          f"{cfg.yamcs_url} ({cfg.instance}/{cfg.processor}) -> "
          f"{cfg.overwatch_url} as {cfg.satellite!r}", flush=True)
    mode, link = cfg.mode, Link()
    while True:
        if mode in ("auto", "ws"):
            try:
                run_ws(cfg, state, progress, link)   # returns on a clean drop
            except Exception as exc:
                print(f"ws subscription failed: {exc}",
                      file=sys.stderr, flush=True)
                # auto falls back only if a subscription NEVER established
                # while YAMCS does answer polls: once proven, a YAMCS restart
                # should meet a reconnect, not a permanent downgrade to
                # polling resolution; a YAMCS still booting is not a refusal.
                if mode == "auto" and not link.established and yamcs_answers(cfg):
                    print("falling back to polling (YAMCS_MODE=auto)",
                          flush=True)
                    mode = "poll"
                    continue
            time.sleep(cfg.poll_seconds)
            continue
        try:
            n = run_once(cfg, state)
            if n:
                progress.add(n)
        except Exception as exc:       # a hiccup must not kill the daemon
            print(f"cycle failed, retrying next poll: {exc}",
                  file=sys.stderr, flush=True)
        time.sleep(cfg.poll_seconds)


if __name__ == "__main__":
    main()
