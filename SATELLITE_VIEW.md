# Control-room views — multi-window, URL-driven (design)

A design for turning Overwatch into a **configurable control room**: several
chrome-less views that sync, laid out across monitors or a video wall, with
the entire layout expressed as URLs. URL-as-state gets ~80% of a control-room
system with essentially no backend — and gives Overwatch something no other
open satellite tracker has: a control room you configure by writing URLs.

## 1. The URL is the view state

One route shape, one param vocabulary. `/w/` means *window*: no header, no
nav, no footer — chrome-less by construction (what you want on a wall).
Keep `/` as the normal app.

```
/w/<view>?<params>
/w/globe?sat=25544&follow=1&rx=1&chrome=0
/w/telemetry?sat=25544&fields=battery_v,battery_i,temp_*&span=6h
/w/passes?sat=25544&station=*
```

Two rules make it work:

1. **URL is authoritative on load.** Everything the view needs is in it —
   no session, no server state, no cookie.
2. **Live changes write back via `history.replaceState`.** An operator
   tweaks a view, copies the URL, pastes it in chat — the colleague sees
   exactly that. That property alone justifies the design.

**Keep URLs readable — do not base64 a JSON blob.** In a control room a URL
an operator can read and hand-edit on a wall machine at 3 a.m. is a feature.
Readable beats compact. Named workspaces (`?ws=nightshift`, a small table)
can come later; not needed for v1.

## 2. Cross-window sync — two tiers

Windows agree on: the selected satellite, the time cursor, the highlighted
event. Which mechanism depends on: **one machine or several?**

- **Tier 1 — one machine, several monitors: `BroadcastChannel`.** Same
  origin, cross-window, zero server, all current browsers.
  ```js
  const bus = new BroadcastChannel('overwatch');
  bus.postMessage({ v: 1, from: winId, type: 'sat', sat: 25544 });
  ```
- **Tier 2 — several machines: server relay.** BroadcastChannel is scoped
  to one browser profile. Use a room id in the URL (`?sync=room:ab3f9k`),
  SSE down, small POST up. **Postgres `LISTEN/NOTIFY` is the elegant
  backing** — it survives blue/green promotes, which an in-memory dict on
  one container would not.

~80 lines total; identical client code behind a `Bus` interface.

**Roles prevent the obvious disasters** — put the role in the URL:
- `role=lead` — emits and receives (operator desk).
- `role=follow` — receives only, never emits (wall displays).
- `role=solo` — ignores the bus (investigating an anomaly alone).

This kills echo loops and stops a stray wall-touchscreen tap from
retargeting six other screens.

**Sync intent, not frames.** Never broadcast per-frame positions. Broadcast
the *state* and let each window compute locally:
```js
{ v:1, type:'time', mode:'replay', anchorWall:1769342400000, anchorSim:1769320000000, rate:4 }
// simNow = anchorSim + (Date.now() - anchorWall) * rate
```
Messages fly only on state change (play/pause/scrub/rate) — a six-screen
room exchanges a dozen messages an hour. Tier-2 caveat: `Date.now()` skews
across machines — have the server stamp each SSE message and let clients
keep a running offset (a crude NTP handshake, ~10 lines) to stay within
tens of ms.

## 3. Multiple monitors and video walls

**Four chromeless quadrants on ONE screen → one fullscreen window, CSS grid
of iframes** (the recommended route; true fullscreen is exclusive per
screen — you cannot have four *fullscreen* windows on one monitor).

```html
<div class="wall">
  <iframe src="/w/globe?sat=25544&chrome=0&role=follow" allow="fullscreen"></iframe>
  <iframe src="/w/telemetry?sat=25544&chrome=0&role=follow" allow="fullscreen"></iframe>
  <iframe src="/w/passes?sat=25544&chrome=0&role=follow" allow="fullscreen"></iframe>
  <iframe src="/w/spacecraft?sat=25544&chrome=0&role=follow" allow="fullscreen"></iframe>
</div>
```
```css
.wall { display:grid; grid-template:1fr 1fr / 1fr 1fr; gap:1px;
        width:100vw; height:100vh; background:#111; }
.wall iframe { border:0; width:100%; height:100%; }
```
- `/wall` is a **shell that consumes the same `/w/` URLs** — any layout is
  still just a URL. Generalize: `grid=2x2|3x1|1x3|2x3` + a `cells=` list, so
  an operator rearranges a wall by editing the address bar.
- **iframes, not direct mounts** — independent reload (reset one
  `iframe.src` to recover a wedged panel; one panel's leak/exception can't
  take the others down). `allow="fullscreen"` → double-click a quadrant to
  zoom it full-screen (one element, legal), double-click to return.
  Same-origin iframes share the `BroadcastChannel` bus.
- **WebGL caveat**: each iframe is its own JS/WebGL context. Chrome caps
  live WebGL contexts (~16) and memory adds up — **at most one 3D
  (spacecraft) panel per wall**; make the others canvas-2D/DOM. For several
  3D panels, drop iframes and share one renderer with
  `setViewport`/`setScissor` across quadrants.

**Other layouts:**
- **Several screens** → one kiosk window per display (`--kiosk <url>`), each
  a different `/w/` URL. Kiosk is chromeless + fullscreen but one window per
  monitor.
- **Hard OS-level isolation** → Chrome app mode (`--app=<url>`, own
  `--user-data-dir` each) tiled by a WM (sway/i3, borders off). OS-specific;
  a deployment, not a URL.
- **One-click room launcher** (Chromium-only, needs a user gesture):
  ```js
  const d = await window.getScreenDetails();     // 'window-management' permission
  d.screens.forEach((s, i) => window.open(layout[i].url, `ow-${i}`,
    `left=${s.availLeft},top=${s.availTop},width=${s.availWidth},height=${s.availHeight}`));
  ```
  Fallback: a page listing the per-screen URLs as big draggable links
  (a printed/bookmarked link sheet is a respectable tier-0).

## 4. What separates "works" from "survives a night shift"

These run for weeks unattended. What actually matters:

- **Data age must be visible and honest.** A frozen display and a live one
  look identical — show the age of the newest frame prominently, amber then
  red past thresholds. In ops this is the single most important element on
  the screen, not polish.
- **Self-healing.** Exponential backoff on SSE reconnect; re-fetch state on
  reconnect rather than assuming continuity.
- **Survive our own deploys.** Blue/green promotes mean a wall holding a
  month-old bundle silently diverges. Carry the build id in the SSE stream,
  compare to the loaded version, schedule a reload on change (with a small
  random delay so twelve screens don't reload in lockstep).
- **Wake lock.** `navigator.wakeLock.request('screen')`, re-acquired on
  `visibilitychange`, or monitors sleep.
- **Leak discipline.** Especially 3D: dispose geometries/textures, cap ring
  buffers, run a 72-hour soak before anyone hangs it on a wall.
- **Dim-room theme.** Control rooms are dark; a white dashboard at 3 a.m.
  is hostile. `?theme=dark|hc`.

## 5. Suggested build order

1. `/w/<view>` routes with `chrome=0` + full URL state + `replaceState`
   write-back. **No sync** — already usable on a wall.
2. `BroadcastChannel` bus with `role=`. Covers one-machine multi-monitor
   (most rooms).
3. Data-age indicator, wake lock, reconnect, version-triggered reload.
4. `sync=room:` over SSE + Postgres `LISTEN/NOTIFY` when someone needs
   multi-machine.
5. Window Management launcher + named workspaces last — conveniences, not
   capabilities.

Steps 1–2 are a couple of days and already deliver the differentiator.

## Why it matters for Overwatch

This is exactly the "control room" the product name promises, and it maps to
the operator/AIT audience (framing B): a sovereign, self-hosted control room
that a NewSpace ops team lays out across their own wall by writing URLs —
no per-tenant custom UI, no backend layout engine. It reuses the existing
views (globe, telemetry, receptions) as `/w/` windows; the tenant model
(TENANT.md) scopes what each window shows.
