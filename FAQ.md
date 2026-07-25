# Overwatch — Frequently asked questions

Plain-language answers for people using Overwatch. For the architecture, see
[SPECIFICATIONS.md](SPECIFICATIONS.md); for the tenancy model,
[TENANT.md](TENANT.md); for what a subscription adds, [PRO.md](PRO.md).

## Why does the map only show around 23 satellites?

This is a deliberate design choice, not a limitation. **Overwatch is
telemetry-only**: a satellite appears on the map only when we can actually
decode its health, right now. Concretely, a satellite is included only if all
three of these hold:

1. **It broadcasts decodable open telemetry** — its beacon is public and not
   encrypted.
2. **A decoder exists for it** — there is a Kaitai Struct definition (a `.ksy`
   file) in the [satnogs-decoders](https://gitlab.com/librespacefoundation/satnogs/satnogs-decoders)
   library that knows how to parse its frames. The community maintains about
   161 of these today.
3. **It was heard recently** — it has SatNOGS frames less than 7 days old that
   decode successfully. A weekly sweep runs every alive catalog satellite
   against every local decoder and promotes the ones that pass this freshness
   gate.

The guiding principle is **honest state**: a satellite we cannot decode, or
have not heard from recently, is shown as *absent* rather than displayed with
faked or empty health. The map answers "which satellites' health can we
actually read right now?", not "what is in orbit?" (there are thousands of
objects up there; almost none broadcast decodable open telemetry).

## Could Overwatch track more satellites? What would be missing?

Yes. There are three independent levers, in rough order of effort:

### 1. More decoders (the real bottleneck)

A satellite with no `.ksy` decoder cannot have its telemetry parsed, so it
cannot appear as a telemetry satellite — no matter how loudly it broadcasts.
The set of open decoders is essentially the
[satnogs-decoders](https://gitlab.com/librespacefoundation/satnogs/satnogs-decoders)
library, so growing coverage means **writing new decoders and contributing
them upstream**. There is no hidden pile of decoders to switch on.

Where new decoders come from:

- **satnogs-decoders** — the canonical library (its `ksy/` directory). This is
  what Overwatch uses directly.
- **SatNOGS DB** ([db.satnogs.org](https://db.satnogs.org)) — each satellite
  entry links to its telemetry decoder; the DB is the community's index of what
  can be decoded.
- **Satellite / cubesat team documentation** — many teams publish a beacon
  interface control document (ICD) or frame format. That document is the raw
  material for writing a new `.ksy`.
- **Kaitai Struct format gallery** ([formats.kaitai.io](https://formats.kaitai.io))
  — background on the `.ksy` format itself (mostly non-satellite).

So "track satellite X" usually reduces to: *does an open decoder exist for it?*
If not, someone has to write one from its published frame format.

### 2. More reception

A satellite that is not heard within 7 days drops off the map — it may be
silent, recently launched and not yet catalogued, or simply outside current
ground-station coverage. Wider reception (more SatNOGS stations, better
coverage of a given orbit) keeps more satellites past the freshness gate.

### 3. Position-only mode (a different product)

We could show far more satellites by tracking *positions only* — propagating
orbits from public elements with no telemetry at all. This is a configuration
switch (`CELESTRAK_GROUPS`), not new engineering. We deliberately keep it off:
thousands of dots with no decoded health would turn Overwatch into a generic
tracker instead of a control room. So "more" is trivial as positions, but it
dilutes the telemetry focus. That is a product decision, not a technical wall.

**One case is permanent:** satellites that broadcast *encrypted* telemetry can
never show decoded health, by nature — no decoder can help.

## Is my data private?

The open-data globe, telemetry, receptions, and public API are open to
everyone. If you push **your own** telemetry into a private tenant, it is
isolated at the database layer (row-level security) — another tenant cannot
read it even with a hand-edited query. Full model in [TENANT.md](TENANT.md);
what a subscription includes in [PRO.md](PRO.md).

## Why don't the reception lines connect to the orbit line?

They do now. Reception links are drawn from a ground station to the satellite's
position at the time it was heard, guarded by a physical horizon limit so no
impossible (over-the-horizon) links are drawn.

## Can I run my own instance?

Yes. Overwatch is open source (AGPL) and self-hostable with a single command —
`docker compose up` on the self-host compose brings up a complete open-data
instance with no configuration. See [README.md](README.md).

## Is there an API? What is free?

Yes — a public REST API under `/api/v1`. All open-data access is free forever.
Private tenant data and managed operation are the paid part; see
[PRO.md](PRO.md).

---

Question not covered here? Contact us at contact@confinia.io.
