"""simsat — a simulated satellite ("devsat") that pushes telemetry into an org (#260).

A flatsat under development IS a satellite without a downlink. This service
stands in for it: it generates plausible spacecraft housekeeping and pushes it
through the same door a real ground segment would use — `POST
/v1/tenants/{key}/telemetry` with a tenant key or org service token. It is a
pure CLIENT: no privileged access, no new ingest path, nothing that can touch
the open-data fleet.

What it simulates (and why it looks right on a dashboard):
- the orbital rhythm: battery voltage/current and panel current track a
  sunlit/eclipse cycle; temperatures follow the illumination with first-order
  thermal inertia, so they lag and round off the way real casings do;
- housekeeping: uptime climbs monotonically and resets on a (rare, simulated)
  reboot that also increments the boot counter and drops the mode to SAFE;
- imperfection: gaussian noise on every analog channel, occasional dropped
  frames, and an optional no-contact gap each orbit — perfect sine waves make
  every dashboard look wrong in a way that teaches nothing;
- faults, on request (SIM_SCENARIO): a slow battery decline, a stuck external
  thermistor, a subsystem going quiet — the shapes an operator wants to SEE.

Configuration (env):
  SIM_KEY        tenant key or org service token (required)
  SIM_BASE       API base, default https://sandbox.api.overwatch.confinia.io
  SIM_SATELLITE  satellite name; the "SIM " prefix is enforced (guardrail:
                 simulated data must be unmistakably simulated)
  SIM_TICK       seconds between frames (default 30)
  SIM_DURATION   stop after this many seconds (default 0 = run forever)
  SIM_SCENARIO   nominal | battery-decline | stuck-thermistor | silent-subsystem
  SIM_PERIOD     orbital period in seconds (default 5560 — a ~550 km LEO)
  SIM_ECLIPSE    eclipse fraction of the orbit (default 0.36)
  SIM_GAPS       "1": add a no-contact gap each orbit (default off)
  SIM_SEED       integer seed for reproducible runs
  SIM_BASIC_USER / SIM_BASIC_PASS   basic-auth gate credentials (sandbox/staging)
  SIM_ALLOW_PROD "1" to allow a production target — refused otherwise

Run it anywhere python3 runs (stdlib only), or as a container:
  podman build -t simsat orbit-poc/simsat
  podman run --rm -e SIM_KEY=… -e SIM_BASIC_USER=… -e SIM_BASIC_PASS=… simsat
"""
from __future__ import annotations

import base64
import json
import math
import os
import random
import sys
import time
import urllib.error
import urllib.request

FIELDS_ANALOG = ("battery_v", "battery_a", "panel_a",
                 "temp_battery_c", "temp_obc_c", "temp_ext_c")


class DevSat:
    """The physics-ish model. Pure and deterministic under a seed: step(dt)
    advances the spacecraft, sample() reads the current housekeeping frame."""

    def __init__(self, seed: int | None = None, period_s: float = 5560.0,
                 eclipse_frac: float = 0.36, scenario: str = "nominal",
                 t0: float = 0.0):
        self.rng = random.Random(seed)
        self.period = float(period_s)
        self.eclipse_frac = min(max(float(eclipse_frac), 0.0), 0.6)
        self.scenario = scenario
        self.t = float(t0)
        self.boot_t = float(t0)
        self.boot_count = 1
        self.mode = "NOMINAL"
        self._safe_until = 0.0
        # state with inertia
        self.battery_v = 7.9
        self.temp_battery = 10.0
        self.temp_obc = 18.0
        self.temp_ext = -5.0
        self._ext_stuck_at: float | None = None
        self._age_days = 0.0

    # --- orbit -----------------------------------------------------------
    def phase(self) -> float:
        return (self.t / self.period) % 1.0

    def sunlit(self) -> bool:
        return self.phase() < (1.0 - self.eclipse_frac)

    def _sun_elevation(self) -> float:
        """0..1 across the sunlit arc — drives panel output."""
        if not self.sunlit():
            return 0.0
        return math.sin(math.pi * self.phase() / (1.0 - self.eclipse_frac))

    # --- dynamics --------------------------------------------------------
    def step(self, dt: float) -> None:
        self.t += dt
        self._age_days += dt / 86400.0

        # battery: relax toward a charge/discharge target
        fade = 0.15 * self._age_days if self.scenario == "battery-decline" else 0.0
        v_full = 8.30 - fade * 0.4
        v_floor = 7.35 - fade
        target = v_full if self.sunlit() else v_floor
        tau_v = 900.0                          # seconds to close ~63 % of the gap
        self.battery_v += (target - self.battery_v) * (1 - math.exp(-dt / tau_v))

        # thermal inertia: casings lag the illumination
        for attr, sun_c, ecl_c, tau in (("temp_battery", 18.0, 2.0, 1500.0),
                                        ("temp_obc", 26.0, 12.0, 1200.0),
                                        ("temp_ext", 22.0, -18.0, 600.0)):
            tgt = sun_c if self.sunlit() else ecl_c
            cur = getattr(self, attr)
            setattr(self, attr, cur + (tgt - cur) * (1 - math.exp(-dt / tau)))

        # the stuck thermistor freezes after the first simulated hour
        if self.scenario == "stuck-thermistor":
            if self._ext_stuck_at is None and (self.t - self.boot_t) > 3600:
                self._ext_stuck_at = round(self.temp_ext, 2)

        # rare reboot: uptime resets, boot counter climbs, SAFE for a while
        p_reboot = 1.0 - math.exp(-dt / (86400.0 * 18))     # ~1 per 18 days
        if self.rng.random() < p_reboot:
            self.boot_t = self.t
            self.boot_count += 1
            self.mode = "SAFE"
            self._safe_until = self.t + 2 * 3600
        elif self.mode == "SAFE" and self.t >= self._safe_until:
            self.mode = "NOMINAL"

    # --- readout ---------------------------------------------------------
    def _noise(self, sigma: float) -> float:
        return self.rng.gauss(0.0, sigma)

    def sample(self) -> dict:
        sun = self.sunlit()
        el = self._sun_elevation()
        charging = sun and self.battery_v < 8.25
        battery_a = (0.85 * el if charging else (0.02 if sun else -0.45))
        frame = {
            "battery_v": round(self.battery_v + self._noise(0.015), 3),
            "battery_a": round(battery_a + self._noise(0.03), 3),
            "panel_a": round(max(0.0, 1.25 * el + self._noise(0.04)), 3),
            "temp_battery_c": round(self.temp_battery + self._noise(0.2), 2),
            "temp_obc_c": round(self.temp_obc + self._noise(0.3), 2),
            "temp_ext_c": round((self._ext_stuck_at if self._ext_stuck_at is not None
                                 else self.temp_ext + self._noise(0.4)), 2),
            "uptime_s": int(self.t - self.boot_t),
            "boot_count": self.boot_count,
            "mode": self.mode,
            "sunlit": 1 if sun else 0,
        }
        if self.scenario == "silent-subsystem" and (self.t - self.boot_t) > 2 * 3600:
            # the OBC thermal channel goes quiet — a hole, not a zero
            frame.pop("temp_obc_c")
        return frame


def frames(sat: DevSat, ticks: int, dt: float, start_ts: float,
           dropout_p: float = 0.02, gaps: bool = False):
    """Yield (unix_ts, frame) advancing the model; a dropped frame yields
    nothing for that tick, a contact gap swallows the last part of each orbit."""
    for i in range(ticks):
        sat.step(dt)
        ts = start_ts + (i + 1) * dt
        if gaps and sat.phase() > 0.85:
            continue                            # out of range of the ground station
        if sat.rng.random() < dropout_p:
            continue                            # a frame lost to the void
        yield ts, sat.sample()


def to_points(ts: float, frame: dict) -> list[dict]:
    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))
    return [{"ts": iso, "field": k, "value": v} for k, v in frame.items()]


# --------------------------------------------------------------------------
# the pushing loop
# --------------------------------------------------------------------------
def _sat_name(raw: str) -> str:
    """Guardrail: simulated data must be unmistakably simulated."""
    name = (raw or "SIM DevSat-1").strip()
    return name if name.upper().startswith("SIM") else f"SIM {name}"


def push(base: str, key: str, satellite: str, points: list[dict],
         basic: str = "") -> dict:
    req = urllib.request.Request(
        f"{base}/v1/tenants/{key}/telemetry",
        data=json.dumps({"satellite": satellite, "points": points}).encode(),
        headers={"Content-Type": "application/json",
                 "User-Agent": "overwatch-simsat/1.0",
                 **({"Authorization": "Basic " + basic} if basic else {})},
        method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def main() -> int:
    key = os.environ.get("SIM_KEY", "").strip()
    if not key:
        print("SIM_KEY (tenant key or org service token) is required", file=sys.stderr)
        return 2
    base = os.environ.get("SIM_BASE",
                          "https://sandbox.api.overwatch.confinia.io").rstrip("/")
    if ("api.overwatch.confinia.io" in base and "sandbox" not in base
            and "staging" not in base
            and os.environ.get("SIM_ALLOW_PROD", "") != "1"):
        print("refusing the production API without SIM_ALLOW_PROD=1 — "
              "simulated data belongs in sandbox/staging first", file=sys.stderr)
        return 2

    satellite = _sat_name(os.environ.get("SIM_SATELLITE", ""))
    tick = max(5.0, float(os.environ.get("SIM_TICK", "30")))
    duration = float(os.environ.get("SIM_DURATION", "0"))
    scenario = os.environ.get("SIM_SCENARIO", "nominal")
    seed_env = os.environ.get("SIM_SEED", "")
    basic = ""
    if os.environ.get("SIM_BASIC_USER"):
        basic = base64.b64encode(
            f"{os.environ['SIM_BASIC_USER']}:{os.environ.get('SIM_BASIC_PASS', '')}"
            .encode()).decode()

    sat = DevSat(seed=int(seed_env) if seed_env else None,
                 period_s=float(os.environ.get("SIM_PERIOD", "5560")),
                 eclipse_frac=float(os.environ.get("SIM_ECLIPSE", "0.36")),
                 scenario=scenario, t0=time.time())
    gaps = os.environ.get("SIM_GAPS", "") == "1"
    print(f"simsat: {satellite!r} -> {base} every {tick:.0f}s "
          f"(scenario={scenario}, gaps={gaps})", flush=True)

    started = time.time()
    sent = 0
    while True:
        for ts, frame in frames(sat, 1, tick, time.time() - tick, gaps=gaps):
            try:
                r = push(base, key, satellite, to_points(ts, frame), basic)
                sent += r.get("accepted", 0)
                print(f"  {time.strftime('%H:%M:%S', time.gmtime(ts))} "
                      f"pushed {r.get('accepted')} points "
                      f"(mode={frame.get('mode')}, sunlit={frame.get('sunlit')}, "
                      f"total={sent})", flush=True)
            except urllib.error.HTTPError as e:
                print(f"  push refused: HTTP {e.code} {e.read().decode()[:120]}",
                      file=sys.stderr, flush=True)
                if e.code in (401, 404, 429):
                    return 1                    # wrong key, gate, or quota: stop
        if duration and time.time() - started >= duration:
            print(f"simsat: done — {sent} points pushed", flush=True)
            return 0
        time.sleep(tick)


if __name__ == "__main__":
    raise SystemExit(main())
