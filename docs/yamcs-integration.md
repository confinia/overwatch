# YAMCS → Overwatch: the full integration workflow

This guide takes you from a running YAMCS instance to your mission's
satellite, telemetry and (where applicable) ground segment visible in the
Overwatch control room. Nothing on the YAMCS side is modified: a small
bridge reads parameters and pushes them into an isolated Overwatch tenant.

```
YAMCS  ──►  bridge  ──►  POST /v1/tenants/{key}/telemetry  ──►  Overwatch store  ──►  control room + mobile
(unchanged)  (poll or WebSocket)         (private tenant, isolated)      (scoped by tenant)
```

See the architecture diagram in the repository README for where this sits
in the whole system.

---

## 0. What you will and will not see

Be clear on the mapping before you start, because the three things an
operator looks for come from different places:

| In the control room | Where it comes from with YAMCS |
| --- | --- |
| **Satellite** (fleet, globe) | Your mission satellite, named by the bridge's `SATELLITE`. Its position on the globe comes from a public catalog (CelesTrak, by NORAD) **or** from your own precise orbit uploaded as a CCSDS OEM (`POST /v1/ephemeris`) for a satellite that is not in public catalogs. |
| **Telemetry** (the "frames") | Every YAMCS parameter update the bridge forwards becomes a telemetry point `(satellite, ts, field, value)`. This is the YAMCS equivalent of a decoded frame's fields: a live, time-stamped stream you view as charts and dashboards. |
| **Antennas / ground stations** | Today the station network on the globe is the **open-data** SatNOGS layer. A private mission's own ground segment (its antennas, its passes) is **not** carried by the telemetry bridge yet — that is a planned mapping. So for a YAMCS mission you get the satellite and its telemetry now; the mission's private ground-station topology is future work. |

In short: **satellite + telemetry are the shipped path; the private
antenna/ground-segment view is planned.** The rest of this guide walks the
shipped path end to end, and points at the ephemeris step that puts a
private satellite on the globe.

---

## 1. Stand up YAMCS

If you already run YAMCS, skip to step 2 and note your instance name,
processor (usually `realtime`) and the parameter qualified names you want.

**Fastest path:** the [demo compose](../orbit-poc/bridge/yamcs/demo/) runs a
quickstart YAMCS, its simulator, and the bridge together, so you can skip
straight to seeing data. The steps below are the manual walkthrough.

To try it from nothing, use the official quickstart:

```sh
git clone https://github.com/yamcs/quickstart
cd quickstart
mvn yamcs:run
```

This starts YAMCS but **emits no telemetry on its own**. The quickstart
ships a simulator that plays back test packets over UDP; run it in a second
terminal, or the parameters stay empty:

```sh
python3 simulator.py     # sends TM to the udp-in link on port 10015
```

- Web UI: <http://localhost:8090>
- Instance: `myproject`, processor `realtime`
- Parameters the simulator populates:
  `/myproject/Battery1_Voltage`, `/myproject/Battery2_Voltage`,
  `/myproject/Battery1_Temp`, `/myproject/Battery2_Temp`, `/myproject/A`

(The instance and parameter names come from the current quickstart; older
versions used `simulator` and `/YSS/SIMULATOR/...`. Check your own instance
under Telemetry → Parameters.)

Confirm values are live in the YAMCS web UI before wiring the bridge.

---

## 2. Get an Overwatch tenant key

The bridge pushes into an **isolated tenant** identified by a key. The key
scopes and meters the private data; it never touches open data or other
tenants.

- **Cloud** (<https://overwatch.confinia.io>): create an organization, then
  a service token from your account (the token is accepted as the tenant
  key). API: `POST /v1/org/tokens`.
- **Self-host**: your own deployment issues its own tenant keys the same
  way; nothing leaves your network.

Keep the key secret. Treat it like a password (see Security below).

---

## 3. Run the bridge

The bridge is a standalone daemon at `orbit-poc/bridge/yamcs/`. Config is
environment-only, so `docker compose up -d` is the whole install.

```sh
cd orbit-poc/bridge/yamcs
# edit docker-compose.example.yml, or set these directly:
```

| Variable | Required | Meaning |
| --- | --- | --- |
| `YAMCS_URL` | yes | Base URL of the YAMCS HTTP API, e.g. `http://yamcs:8090` |
| `YAMCS_INSTANCE` | yes | YAMCS instance name (`myproject` in the quickstart) |
| `YAMCS_PROCESSOR` | no | Processor to read (default `realtime`) |
| `YAMCS_PARAMETERS` | yes | Comma-separated parameter qualified names |
| `YAMCS_FIELD_MAP` | no | Comma-separated `qname=field` overrides for nicer names |
| `OVERWATCH_URL` | yes | Overwatch base URL. Cloud: `https://overwatch.confinia.io/api`. Self-host: your api's base. If the bridge runs on the same host as Overwatch, point it at the internal service address, not the public URL (a host often cannot reach its own public edge). |
| `TENANT_KEY` | yes | Your tenant key from step 2 |
| `SATELLITE` | yes | The name your satellite appears under |
| `POLL_SECONDS` | no | Poll interval, and the reconnect pause in ws mode (default 10) |
| `YAMCS_MODE` | no | `auto` (default), `ws` or `poll` |

Example, against the quickstart simulator:

```yaml
services:
  yamcs-bridge:
    build: { context: .., dockerfile: yamcs/Dockerfile }
    restart: unless-stopped
    environment:
      YAMCS_URL: http://host.docker.internal:8090
      YAMCS_INSTANCE: myproject
      YAMCS_PROCESSOR: realtime
      YAMCS_PARAMETERS: >-
        /myproject/Battery1_Voltage,/myproject/Battery2_Voltage,
        /myproject/Battery1_Temp,/myproject/Battery2_Temp,/myproject/A
      OVERWATCH_URL: https://overwatch.confinia.io/api
      TENANT_KEY: <your tenant key>
      SATELLITE: QuickSat
      YAMCS_MODE: auto
```

```sh
docker compose -f docker-compose.example.yml up -d
docker compose logs -f yamcs-bridge
```

### How the bridge reads YAMCS

- `YAMCS_MODE=auto` (default) opens the YAMCS **WebSocket** `parameters`
  subscription and receives every update as it happens. If the subscription
  never establishes (a proxy that strips WebSocket, an old server), it falls
  back to **polling** `parameters:batchGet` every `POLL_SECONDS`. Once a
  subscription is proven, a drop reconnects instead of downgrading.
- Force one behaviour with `YAMCS_MODE=ws` or `YAMCS_MODE=poll`.
- The bridge remembers the last generation time per parameter and pushes
  only new samples; a restart re-pushes at most one sample each, and the
  Overwatch upsert makes replays harmless.

### Field naming

A parameter's basename becomes the telemetry field:
`/myproject/Battery1_Voltage` → `Battery1_Voltage`. Override collisions or
give nicer names with `YAMCS_FIELD_MAP`, e.g.
`YAMCS_FIELD_MAP=/myproject/A=alpha_deg`.

---

## 4. Verify ingestion

Two checks confirm data is flowing:

1. **Bridge logs** show pushes:
   ```
   yamcs-bridge [auto]: 5 parameters from http://... -> https://... as 'QuickSat'
   pushed 5 points
   ```
2. **Tenant API** shows the satellite and its fields:
   ```sh
   curl -s "$OVERWATCH_URL/v1/tenants/$TENANT_KEY/satellites"
   # -> [{"satellite":"QuickSat","field":"Battery1_Voltage","points":123,"last":"..."}, ...]

   curl -s "$OVERWATCH_URL/v1/tenants/$TENANT_KEY/telemetry?satellite=QuickSat&field=Battery1_Voltage&hours=1"
   # -> [{"ts":"...","value":12.1}, ...]
   ```

If `/satellites` lists your satellite and fields, ingestion works.

---

## 5. See it in Overwatch

### Satellite

Your satellite (`SATELLITE`) is now a satellite in your tenant, carrying the
parameters you forwarded.

To place it **on the globe**:

- If the satellite is in the public catalog, it is tracked by NORAD
  automatically.
- If it is a private mission not in public catalogs, upload your own precise
  orbit as a CCSDS **OEM** ephemeris:
  ```sh
  curl -X POST "$OVERWATCH_URL/v1/ephemeris" \
       -H "Content-Type: text/plain" --data-binary @mission.oem
  ```
  Overwatch then propagates positions and passes from your ephemeris instead
  of a public TLE.

### Telemetry (the "frames")

Each YAMCS parameter update is a telemetry point over time — the YAMCS
analog of a decoded frame's fields. View it as:

- **Time series** via the tenant API (`/v1/tenants/{key}/telemetry`), which
  is also what the dashboards query.
- **Dashboards**: the private-telemetry panels read the same series, so a
  battery, a temperature, a mode reads the same way a mission operator's
  YAMCS display shows it — now inside the Overwatch control room.

### Ground stations / antennas

The station network and passes on the globe are the open-data (SatNOGS)
layer today. A private mission's own antennas and passes are a planned
mapping, not carried by the telemetry bridge yet. Track that work in the
project's issues.

---

## 6. Troubleshooting

| Symptom | Likely cause and fix |
| --- | --- |
| Bridge logs `parameters YAMCS does not know` | A qualified name is wrong or renamed. The subscription keeps running (abortOnInvalid is off); fix the name in `YAMCS_PARAMETERS`. |
| No pushes, `ws subscription failed` then polling | WebSocket is blocked between the bridge and YAMCS. Polling still works; or set `YAMCS_MODE=poll`. |
| `429 Daily ingest quota reached` | The tenant's daily point quota is hit. Reduce parameters or poll interval, or raise the quota. |
| `403 ... plan includes one satellite` | A single-satellite plan received a second satellite name. Use one `SATELLITE`, or upgrade the tenant. |
| `/satellites` empty | The bridge is not pushing — check its logs, `OVERWATCH_URL`, and `TENANT_KEY`. |

---

## 7. Self-host vs cloud

The workflow is identical. For a fully on-premise mission:

- run YAMCS, the bridge and Overwatch on the same network,
- point `OVERWATCH_URL` at your Overwatch service,
- nothing leaves your network.

This is the natural fit: on-premise MCS, on-premise Overwatch, on-premise
control room.

---

## 8. Security

- The bridge only **reads** YAMCS and **writes** telemetry. It cannot send
  commands. Overwatch shows; the MCS commands.
- Keep `TENANT_KEY` secret: pass it by environment or a secrets manager,
  never commit it. It scopes writes to one isolated tenant.
- All transport is HTTPS to Overwatch; use TLS to YAMCS where possible.

---

## 9. What comes next

- **WebSocket-only refinements** and back-pressure tuning.
- **Other MCS behind the same seam**: SCOS-2000, and CCSDS MO via the ESA
  NanoSat MO Framework — the same `Sample(name, ts, value)` contract.
- **A live public demo** of Overwatch driven by a running YAMCS.
- **Private telemetry on mobile**: monitor your tenant's satellites and
  fields from the phone.
- **Private ground segment**: bringing a mission's own antennas and passes
  into the view (the missing piece from step 5).

See the project's issue tracker for the status of each.
