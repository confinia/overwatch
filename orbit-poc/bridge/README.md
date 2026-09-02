# MCS bridges: the adapter contract

Overwatch sits downstream of a Mission Control System and shows the
control-room view; a **bridge** is the pipe between one MCS and an
Overwatch tenant. [core.py](core.py) is the MCS-neutral half every
bridge shares; an **adapter** is the MCS-shaped half. YAMCS
([yamcs/](yamcs/)) is the first adapter; this page is the contract a
second one implements — written so that a partner holding a licensed,
closed-source MCS (SCOS-2000 being the canonical case,
[#425](https://github.com/confinia/overwatch/issues/425)) can build and
validate one without any change to Overwatch.

## The contract

An adapter produces `Sample` values:

| Field | Meaning |
| --- | --- |
| `name` | The source-side identifier, verbatim (a YAMCS qualified name, a SCOS parameter mnemonic). Also the dedupe key. |
| `ts` | ISO 8601 generation time, as stamped on board or by the MCS — not the time of reading. |
| `value` | Already flattened: a number, or text for anything that is not one (enum states, strings). Booleans as 1/0. |

That is the whole interface. The core then guarantees:

- **Dedupe**: a `(name, ts)` already pushed is never pushed again in
  this process; across restarts, the tenant endpoint's upsert on
  `(satellite, ts, field)` makes any replay harmless. Adapters may
  therefore re-deliver freely (a cache resend after reconnect, a file
  re-read) — idempotence is the core's problem, not theirs.
- **Field naming**: the last `/`-segment of `name` by default, explicit
  overrides via a mapping for collisions or nicer names.
- **Delivery**: chunked `POST /v1/tenants/{key}/telemetry` at the API's
  1000-point limit, quota errors surfaced, nothing dropped silently.

How the adapter obtains samples is its own business: a live
subscription, a poll loop, or a file export replay — the YAMCS adapter
does the first two ([#424](https://github.com/confinia/overwatch/issues/424)),
and for an MCS that exports TM archives to files, a replay adapter is
often the shortest path to first light.

## What a SCOS-2000 partner would do

1. Pick the extraction surface their installation already offers
   (archive retrieval, an external interface, a file export).
2. Write the ~50 lines turning that into `Sample(name, ts, value)`.
3. Reuse `core.to_points` + `core.push` for everything else, or simply
   copy the yamcs daemon's `main` shape.
4. Validate against their instance; Overwatch needs nothing new — the
   tenant API is the same one every bridge and every user already talks
   to.

Friction found on the way (interfaces that do not behave as documented)
is exactly the feedback worth having — open an issue here, whatever the
MCS.
