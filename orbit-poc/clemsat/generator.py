"""CLEMSAT-1 — a fake-satellite telemetry generator (issue #27).

Simulates a real satellite in operation: a physically-plausible telemetry
stream (battery, temperatures, current, mode) driven by a modelled orbit
day/eclipse cycle, pushed to a private tenant through the PUBLIC API exactly
as a real ground segment would. Optional, standalone, env-gated — never
part of the core data path.

Purpose: partner demos (a moving fleet before real telemetry exists),
the end-user journey, and an always-alive reference tenant.

Env:
  API_BASE      default https://overwatch.confinia.io
  TENANT_TOKEN  required — tenant key or org service token to push under
  SATELLITE     default CLEMSAT-1
  CADENCE_S     default 30 (seconds between pushes)
"""
import math
import os
import random
import time

import requests

API_BASE = os.environ.get("API_BASE", "https://overwatch.confinia.io")
# On-VM runs push through the internal app caddy (rootless containers can't
# hairpin to the public IP); HOST sets the vhost so caddy routes correctly.
# External ground segments just use the public https URL (no HOST needed).
HOST = os.environ.get("HOST", "")
TOKEN = os.environ["TENANT_TOKEN"]
SAT = os.environ.get("SATELLITE", "CLEMSAT-1")
CADENCE = int(os.environ.get("CADENCE_S", "30"))

PERIOD_S = 95 * 60          # ~95 min LEO orbit
ECLIPSE_FRAC = 0.38         # ~36 min in shadow

# Persistent state (a real satellite's values evolve, they aren't redrawn).
battery_v = 7.9
temp_obc = 18.0
temp_pa = 20.0
uptime = 0
reset_count = 3
t0 = time.time()


def _approach(value, target, rate, noise):
    """Move a state variable toward a target with first-order lag + noise."""
    return value + (target - value) * rate + random.uniform(-noise, noise)


def sample():
    """One physically-plausible telemetry sample from the orbit phase."""
    global battery_v, temp_obc, temp_pa, uptime, reset_count
    now = time.time()
    uptime = int(now - t0)
    phase = (now % PERIOD_S) / PERIOD_S
    sunlit = phase < (1.0 - ECLIPSE_FRAC)

    if sunlit:
        battery_v = _approach(battery_v, 8.25, 0.06, 0.01)   # charging
        battery_i = round(random.uniform(0.35, 0.65), 3)     # into the battery
        temp_obc = _approach(temp_obc, 26.0, 0.05, 0.3)
        temp_pa = _approach(temp_pa, 30.0, 0.05, 0.4)
    else:
        battery_v = _approach(battery_v, 7.15, 0.05, 0.01)   # discharging
        battery_i = round(-random.uniform(0.25, 0.5), 3)
        temp_obc = _approach(temp_obc, 2.0, 0.05, 0.3)
        temp_pa = _approach(temp_pa, -5.0, 0.05, 0.4)

    mode = "nominal"
    # Rare injected anomaly: a brief safe-mode + reset, for realistic dashboards.
    if random.random() < 0.004:
        mode = "safe"
        reset_count += 1
        battery_v = _approach(battery_v, 6.8, 0.5, 0.02)

    battery_pct = max(0, min(100, round((battery_v - 6.8) / (8.4 - 6.8) * 100)))
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    return [
        {"ts": ts, "field": "battery_v", "value": round(battery_v, 3)},
        {"ts": ts, "field": "battery_i", "value": battery_i},
        {"ts": ts, "field": "battery_pct", "value": battery_pct},
        {"ts": ts, "field": "temp_obc", "value": round(temp_obc, 2)},
        {"ts": ts, "field": "temp_pa", "value": round(temp_pa, 2)},
        {"ts": ts, "field": "sunlit", "value": 1 if sunlit else 0},
        {"ts": ts, "field": "uptime_s", "value": uptime},
        {"ts": ts, "field": "reset_count", "value": reset_count},
        {"ts": ts, "field": "mode", "value": mode},
    ]


def main():
    url = f"{API_BASE}/api/v1/tenants/{TOKEN}/telemetry"
    print(f"CLEMSAT-1 generator → {url} (sat={SAT}, every {CADENCE}s)")
    while True:
        try:
            headers = {"Host": HOST} if HOST else {}
            r = requests.post(url, json={"satellite": SAT, "points": sample()},
                              headers=headers, timeout=15)
            print(f"{time.strftime('%H:%M:%S')} push {r.status_code} "
                  f"batt={battery_v:.2f}V", flush=True)
        except Exception as e:                      # a demo generator never crashes
            print(f"push failed: {e}", flush=True)
        time.sleep(CADENCE)


if __name__ == "__main__":
    main()
