"""YAMCS -> Overwatch bridge (#423).

A standalone poller: reads parameter values from a YAMCS processor over the
HTTP API (`parameters:batchGet`) and pushes them into an Overwatch tenant
through `POST /v1/tenants/{key}/telemetry`. The MCS is not modified; the
Overwatch API is not modified. The bridge is only the pipe between two
seams that already exist.

Polling REST, on purpose: version-tolerant, debuggable with curl. The
WebSocket subscription (#424) is a drop-in upgrade of the pull loop only.

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
    POLL_SECONDS      10                           (default: 10)
"""

import os
import sys
import time
from dataclasses import dataclass, field

import requests

PUSH_CHUNK = 1000          # the tenant endpoint's hard batch limit


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


@dataclass
class State:
    """Last generationTime pushed, per parameter. A restart re-pushes at most
    one sample per parameter; the tenant endpoint upserts on (satellite, ts,
    field), so replays are harmless."""
    last: dict[str, str] = field(default_factory=dict)


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


def field_name(qname: str, field_map: dict[str, str]) -> str:
    """Basename by default (/YSS/SIMULATOR/BatteryVoltage1 -> BatteryVoltage1),
    explicit override for collisions or nicer names."""
    return field_map.get(qname) or qname.rsplit("/", 1)[-1]


def fetch(cfg: Config) -> list[dict]:
    """One batchGet against the processor: what an operator's display sees."""
    url = (f"{cfg.yamcs_url}/api/processors/{cfg.instance}/{cfg.processor}"
           "/parameters:batchGet")
    body = {"id": [{"name": q} for q in cfg.parameters]}
    r = requests.post(url, json=body, timeout=15)
    r.raise_for_status()
    data = r.json()
    return data.get("value") or data.get("values") or []


def to_points(values: list[dict], cfg: Config, state: State) -> list[dict]:
    """New-samples-only conversion; advances state as it goes."""
    points = []
    for pv in values:
        qname = (pv.get("id") or {}).get("name")
        gen = pv.get("generationTime")
        eng = pv.get("engValue") or pv.get("rawValue")
        if not qname or not gen or eng is None:
            continue
        if state.last.get(qname) == gen:
            continue                    # already pushed this sample
        value = scalar(eng)
        if value is None:
            continue
        state.last[qname] = gen
        points.append({"ts": gen,
                       "field": field_name(qname, cfg.field_map),
                       "value": value})
    return points


def push(cfg: Config, points: list[dict]) -> int:
    """Chunked pushes into the tenant; returns points accepted."""
    accepted = 0
    for i in range(0, len(points), PUSH_CHUNK):
        chunk = points[i:i + PUSH_CHUNK]
        r = requests.post(
            f"{cfg.overwatch_url}/v1/tenants/{cfg.tenant_key}/telemetry",
            json={"satellite": cfg.satellite, "points": chunk}, timeout=30)
        r.raise_for_status()
        accepted += r.json().get("accepted", len(chunk))
    return accepted


def run_once(cfg: Config, state: State) -> int:
    """One poll cycle. Returns points pushed. Raises on transport errors;
    the loop catches, the tests call it directly."""
    points = to_points(fetch(cfg), cfg, state)
    return push(cfg, points) if points else 0


def main() -> None:
    cfg = load_config()
    state = State()
    print(f"yamcs-bridge: {len(cfg.parameters)} parameters from "
          f"{cfg.yamcs_url} ({cfg.instance}/{cfg.processor}) -> "
          f"{cfg.overwatch_url} as {cfg.satellite!r}, every "
          f"{cfg.poll_seconds:g}s", flush=True)
    while True:
        try:
            n = run_once(cfg, state)
            if n:
                print(f"pushed {n} points", flush=True)
        except Exception as exc:       # a hiccup must not kill the daemon
            print(f"cycle failed, retrying next poll: {exc}",
                  file=sys.stderr, flush=True)
        time.sleep(cfg.poll_seconds)


if __name__ == "__main__":
    main()
