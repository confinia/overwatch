# YAMCS → Overwatch bridge

Connects a [YAMCS](https://yamcs.org) mission control system to Overwatch:
the bridge polls parameter values from a YAMCS processor and pushes them
into your Overwatch tenant, where they show up next to everything else the
control room sees. Your MCS is not modified. Nothing else is required.

Self-hosting is the natural fit: on-premise MCS, on-premise Overwatch,
nothing leaves the network. It works identically against the cloud
instance at https://overwatch.confinia.io.

**Full walkthrough**, from a YAMCS quickstart to the satellite and its
telemetry in the control room: [YAMCS → Overwatch integration guide](../../../docs/yamcs-integration.md).

## Run it

```sh
cd orbit-poc/bridge/yamcs
# edit docker-compose.example.yml: your YAMCS address, your tenant key
docker compose -f docker-compose.example.yml up -d
```

No YAMCS at hand? The [YAMCS quickstart](https://github.com/yamcs/quickstart)
runs a full simulated mission (`mvn yamcs:run`, web UI on port 8090) whose
parameters the example compose file already lists.

## What maps to what

| YAMCS | Overwatch |
| --- | --- |
| parameter qualified name `/YSS/SIMULATOR/BatteryVoltage1` | telemetry field `BatteryVoltage1` (basename; override via `YAMCS_FIELD_MAP`) |
| `generationTime` | point timestamp `ts` |
| `engValue` (raw value as fallback) | point `value` — numeric types as numbers, booleans as 1/0, everything else as text |
| the processor (default `realtime`) | what gets pushed: the same values an operator's YAMCS display shows |

The bridge remembers the last `generationTime` per parameter and pushes
only newer samples. Overwatch upserts on `(satellite, ts, field)`, so a
bridge restart never duplicates data.

## Environment reference

| Variable | Required | Meaning |
| --- | --- | --- |
| `YAMCS_URL` | yes | Base URL of the YAMCS HTTP API, e.g. `http://yamcs:8090` |
| `YAMCS_INSTANCE` | yes | YAMCS instance name |
| `YAMCS_PROCESSOR` | no | Processor to read (default `realtime`) |
| `YAMCS_PARAMETERS` | yes | Comma-separated parameter qualified names |
| `YAMCS_FIELD_MAP` | no | Comma-separated `qname=field` overrides |
| `OVERWATCH_URL` | yes | Overwatch base URL (cloud or your self-host) |
| `TENANT_KEY` | yes | Your tenant key (or an org service token) |
| `SATELLITE` | yes | Satellite name the points are filed under |
| `POLL_SECONDS` | no | Poll interval, and the reconnect pause in ws mode (default 10) |
| `YAMCS_MODE` | no | `auto` (default), `ws` or `poll` — see below |

## Live subscription vs polling

By default (`YAMCS_MODE=auto`) the bridge subscribes to the YAMCS
WebSocket `parameters` topic and receives every update as it happens. If
the subscription never establishes (a proxy that strips WebSocket, an old
server), it falls back to polling `parameters:batchGet` every
`POLL_SECONDS` — same data, resolution bounded by the interval, since
between polls only the latest sample per parameter is seen. Force one
behavior with `YAMCS_MODE=ws` or `YAMCS_MODE=poll`. A dropped
subscription reconnects by itself; the per-parameter dedupe makes the
reconnect's cache resend harmless.

## Limits

- In `poll` mode, resolution is bounded by the poll interval (see above).
- The tenant API accepts 1000 points per request; the bridge chunks
  automatically. Daily ingest quotas are the tenant's, not the bridge's.
- Other MCS flavors: the seam is designed to take more adapters;
  SCOS-2000 is [#425](https://github.com/confinia/overwatch/issues/425).
