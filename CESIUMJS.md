# MapLibre vs CesiumJS — engine choice for Overwatch

Decision record for the globe engine (issue #43). **We keep MapLibre.**
This weighs both against *our* needs: a real-time satellite globe + vector
overlays (positions, ground tracks, reception lines), embedded next to
Grafana, working on phones, self-hosted in Europe, open-source.

## Our actual needs

- A 3D globe showing ~tens of satellites + orange reception lines + ground
  tracks, updated every 15 s.
- Runs well on a smartphone (iPhone-class) — a real requirement (mobile
  users hit the public site).
- Vector overlays (GeoJSON points/lines) with click/hover interaction.
- Raster basemap (Sentinel-2 tiles from EOX).
- Open-source, no US/commercial-service dependency (sovereignty story).
- Small bundle, simple self-hosting.
- We are NOT doing: terrain, 3D tiles/buildings, sub-metre precision,
  time-dynamic CZML playback, or heavy geospatial analysis.

## Comparison

| Criterion | MapLibre GL JS | CesiumJS |
|---|---|---|
| Engine type | Vector map, globe projection added | Full 3D geospatial (ECEF) engine |
| Bundle / weight | Light | Heavy (larger JS, more GPU/memory) |
| Mobile (iPhone-class) | Good | Heavier; can strain low/mid phones |
| Pole navigation | Known limitation — no camera across poles, approximations near poles (feels inverted/buggy) | Native true-3D camera — no pole singularity |
| Vector overlays (GeoJSON) | First-class, simple | Supported (entities), heavier model |
| Raster basemap tiles | First-class | Supported (imagery layers) |
| Terrain / 3D tiles | Limited | Excellent (its core strength) |
| Time-dynamic data (CZML) | None (we precompute + setData) | First-class (interpolated intervals) |
| Open-source & governance | Fully OSS, community-governed | OSS (Apache-2), but ecosystem leans to commercial Cesium ion |
| Sovereignty fit | Strong (no US service in the path) | Weaker (ion tiles/assets are US-hosted; avoidable but tempting) |
| Our team | **Founder is a MapLibre contributor**, used elsewhere | No in-house expertise |
| Already integrated | Yes | No — would be a rewrite |

## Pros / cons for us

**MapLibre — pros**
- Light and mobile-friendly — matches the public-site requirement.
- Founder is a contributor: fixes go upstream, aligns with the brand, and
  the open-source/sovereign story is coherent.
- Already built, integrated, and shipping.
- Vector-first: our overlays (positions, lines, stations) are its bread
  and butter.

**MapLibre — cons**
- Pole navigation feels inverted/buggy (documented limitation): near the
  N/S poles the drag→rotation mapping is approximate. Annoying, but an
  edge case, and **improvable upstream** (see #43).
- No native time-dynamic playback — a non-issue for us (we precompute
  positions server-side and `setData`).

**CesiumJS — pros**
- True 3D camera: no pole singularity — the pole-navigation annoyance
  would simply not exist.
- Best-in-class terrain / 3D tiles / time-dynamic CZML — none of which we
  need today.

**CesiumJS — cons**
- Heavier bundle + GPU/memory: a real cost on phones.
- Large rewrite for us; no in-house expertise.
- Ecosystem pull toward commercial, US-hosted ion services — friction with
  the sovereignty positioning.
- Overkill: we'd adopt a heavy 3D-geospatial engine to fix one navigation
  edge case.

## Conclusion

Keep **MapLibre**: lighter (especially mobile), already integrated,
open-source and sovereignty-aligned, and maintained by a team that
includes a MapLibre contributor. The only real MapLibre weakness for us —
pole navigation — is an edge case that is *improvable in MapLibre itself*
(file a focused upstream issue with a repro; consider a PR clamping/adjusting
the near-pole drag→rotation mapping — #43). Switching to Cesium would trade
a mobile-perf regression and a rewrite for a single edge-case fix.

Revisit only if a future need appears that MapLibre genuinely cannot serve
(true terrain, 3D tiles, or interpolated time-dynamic playback) — none of
which are on the roadmap.
