"""The MCS-neutral half of every bridge (#425).

An adapter (YAMCS today, SCOS-2000 or anything else tomorrow) produces
Samples; this core handles everything after: per-name dedupe, field
naming, and the chunked push into an Overwatch tenant. The contract an
adapter implements is documented in README.md next to this file — it is
deliberately tiny, because the whole point of the seam is that a partner
with a licensed closed-source MCS can implement it in an afternoon.
"""

from dataclasses import dataclass, field

import requests

PUSH_CHUNK = 1000          # the tenant endpoint's hard batch limit


@dataclass(frozen=True)
class Sample:
    """One telemetry value as any MCS hands it over.

    name  — the source-side identifier (a YAMCS qualified name, a SCOS
            parameter mnemonic, ...); also the dedupe key.
    ts    — ISO 8601 generation time, as produced on board / by the MCS.
    value — already flattened: a number, or text for everything else.
    """
    name: str
    ts: str
    value: float | str


@dataclass
class State:
    """Last generation time pushed, per name. A restart re-pushes at most
    one sample per name; the tenant endpoint upserts on (satellite, ts,
    field), so replays are harmless."""
    last: dict[str, str] = field(default_factory=dict)


def field_name(name: str, field_map: dict[str, str]) -> str:
    """Basename by default (/YSS/SIMULATOR/BatteryVoltage1 ->
    BatteryVoltage1; a name without slashes stays itself), explicit
    override for collisions or nicer names."""
    return field_map.get(name) or name.rsplit("/", 1)[-1]


def to_points(samples: list[Sample], field_map: dict[str, str],
              state: State) -> list[dict]:
    """New-samples-only conversion to tenant points; advances state."""
    points = []
    for s in samples:
        if state.last.get(s.name) == s.ts:
            continue                    # already pushed this sample
        state.last[s.name] = s.ts
        points.append({"ts": s.ts,
                       "field": field_name(s.name, field_map),
                       "value": s.value})
    return points


def push(overwatch_url: str, tenant_key: str, satellite: str,
         points: list[dict]) -> int:
    """Chunked pushes into the tenant; returns points accepted."""
    accepted = 0
    for i in range(0, len(points), PUSH_CHUNK):
        chunk = points[i:i + PUSH_CHUNK]
        r = requests.post(
            f"{overwatch_url}/v1/tenants/{tenant_key}/telemetry",
            json={"satellite": satellite, "points": chunk}, timeout=30)
        r.raise_for_status()
        accepted += r.json().get("accepted", len(chunk))
    return accepted
