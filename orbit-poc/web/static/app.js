// Overwatch control room — application code.
//
// Extracted from index.html for MapLibre 6 (#145): v6 ships ESM only, so the
// library is imported by a module in the page, which then injects THIS file as
// a classic script. That ordering matters twice: the code below needs
// `maplibregl` to already exist, and its top-level functions must stay global
// because the markup wires them through inline onclick handlers.
// API + Grafana bases. Same-origin by default (VM behind caddy); the GitHub
// Pages demo mirror (confinia.github.io/overwatch) overrides API_BASE to the
// VM origin — caddy sends CORS + frame-ancestors for that origin.
// Kiosk mode strips Grafana chrome; the cookie/auth-proxy layer (production)
// would sit in front of this.
const LOCAL = ["localhost", "127.0.0.1"].includes(location.hostname);
const MIRROR = location.hostname.endsWith("github.io");
const API_BASE = MIRROR ? "https://overwatch.confinia.io" : "";
// Grafana is always same-origin under /grafana (behind caddy in every
// deployment: production, staging, and self-host). LOCAL kept for the beacon.
const GRAFANA = `${API_BASE}/grafana`;
const DASH_UID = "orbit-telemetry";
// #105: on the very first (cold) visit the Grafana anonymous session isn't
// established, so d-solo iframes can paint the Grafana home page ("Welcome to
// Grafana") instead of the panel — exactly what a manual reload fixes. We do
// that reload once, automatically, until the session is warm.
let gfReady = false;
// Coarse pointer (phones/tablets): draw visibly bigger dots — a 4px target
// is neither tappable nor perceived as tappable on a phone.
const TOUCH = window.matchMedia("(pointer: coarse)").matches;

// Time window (hours) for receptions, decoded fields and track — one range
// drives all three so they describe the same frames over the same window
// (#70/#71/#72). Persisted per browser.
const RANGES = [[1,"1h"],[6,"6h"],[24,"24h"],[72,"3d"],[168,"7d"]];
let rangeHours = Number(localStorage.getItem("ovw_rangeHours"));
if (!RANGES.some(r => r[0] === rangeHours)) rangeHours = 168;   // default 7 days
function renderRangebar(){
  const el = document.getElementById("rangebar");
  if (!el) return;
  el.innerHTML = `<span class="lbl">Range</span>` + RANGES.map(([h, l]) =>
    `<button class="${h === rangeHours ? "on" : ""}" onclick="setRange(${h})">${l}</button>`).join("");
}
function setRange(h){
  if (h === rangeHours) return;
  rangeHours = h; localStorage.setItem("ovw_rangeHours", String(h));
  renderRangebar();
  // re-fetch the active view with the new window so map + fields stay coherent
  if (activeStation) selectStation(activeStation);
  else if (activeNorad != null && satsByNorad[activeNorad]){
    const s = satsByNorad[activeNorad];
    drawTrack(s.norad); drawReceptions(s.norad); embedDashboards(s);
  }
}
renderRangebar();

// Account: same OpenID token (cookie) as the API and Grafana. Signed out ->
// sign-in/register link (Keycloak handles both); signed in without org ->
// create-organization action; with org -> private fleet section in the list.
let orgInfo = null;
let signedIn = false;                 // #221: favourites are a signed-in feature
let myFavorites = new Set();          // norads the user starred
// View mode: anonymous -> always the full open-data fleet. Signed in with
// an organization -> private fleet only by DEFAULT; open data is an
// activable option (persisted per browser).
let showOpen = true;
function favouriteOpenSats(){
  // #255: "hide open-data fleet" means "show only what I follow", not "show
  // nothing" — a starred satellite stays visible in the list and on the globe.
  return signedIn ? [...myFavorites].filter(n => satsByNorad[n]) : [];
}
function applyOpenVisibility(){
  const favs = favouriteOpenSats();
  const keepSome = !showOpen && favs.length > 0;
  const vis = (showOpen || keepSome) ? "visible" : "none";
  for (const l of ["sats","sats-hit","sat-labels","track","track-arcs","rx-links",
                   "rx-links-glow","rx-endpoints","rx-links-hit","rx-stations","rx-stations-hit","rx-station-labels"]){
    try { if (map.getLayer(l)) map.setLayoutProperty(l, "visibility", vis); } catch(e){}
  }
  // In "only my satellites" mode the dot layers are filtered to the favourites
  // rather than hidden; the reception/track layers follow the selection anyway.
  for (const l of ["sats","sats-hit","sat-labels"]){
    try {
      if (!map.getLayer(l)) continue;
      map.setFilter(l, keepSome
        ? ["in", ["get","norad"], ["literal", favs]] : null);
    } catch(e){}
  }
  const fb = document.getElementById("fleetbar");
  if (fb) fb.style.display = showOpen ? "flex" : "none";
}
function toggleOpen(){
  showOpen = !showOpen;
  localStorage.setItem("ovw_showOpen", showOpen ? "1" : "0");
  // Hiding the fleet used to leave the selected open-data satellite on screen —
  // its panel, legend and track stayed while it vanished from the list (#255).
  // Keep it only if it is one the user actually follows.
  if (!showOpen && activeNorad != null && !myFavorites.has(activeNorad)){
    activeNorad = null;
    setRxLegend("");
    const empty = { type:"FeatureCollection", features: [] };
    for (const src of ["track","track-arcs","rx-links","rx-stations","rx-endpoints"]){
      const o = map.getSource(src);
      if (o) { try { o.setData(empty); } catch(e){} }
    }
    const head = document.getElementById("panelHead");
    const body = document.getElementById("panelBody");
    if (head) head.textContent = "Select a satellite to inspect its telemetry";
    if (body) body.innerHTML = `<div class="empty">Showing your satellites only. ` +
      `Star an open-data satellite (★) to keep following it here.</div>`;
    if (location.hash) history.replaceState(null, "", location.pathname);
  }
  applyOpenVisibility(); renderList(allSats);
}
async function loadAccount(){
  const el = document.getElementById("acct");
  try {
    const r = await fetch(`${API_BASE}/api/v1/me`);
    if (!r.ok) throw 0;
    const me = await r.json();
    signedIn = true;                                          // #221
    myFavorites = new Set((me.satellites || []).map(x => x.norad));
    if (me.organization){
      el.innerHTML = ` · <b>${me.organization.name}</b> ` +
        `<a class="action" href="/w/account">account</a> · ` +
        `<a href="#" onclick="deleteOrg('${me.organization.id}','${me.organization.name}');return false" style="color:var(--dim)">delete org</a> · ` +
        `<a href="#" onclick="signOut();return false" style="color:var(--dim)">sign out</a>`;
      const inv = await (await fetch(`${API_BASE}/api/v1/org/satellites`)).json();
      const by = {};
      for (const row of inv) (by[row.satellite] = by[row.satellite] || []).push(row);
      orgInfo = { name: me.organization.name,
                  sats: Object.entries(by).map(([sat, f]) => ({ satellite: sat, fields: f })) };
      showOpen = localStorage.getItem("ovw_showOpen") === "1";   // default: private only
    } else {
      el.innerHTML = ` · ${me.email || "signed in"} — ` +
        `<a class="action" href="#" onclick="createOrg();return false">Create your organization</a> · ` +
        `<a href="/w/account" style="color:var(--dim)">account</a> · ` +
        `<a href="#" onclick="signOut();return false" style="color:var(--dim)">sign out</a>`;
      orgInfo = null;
    }
  } catch (e) {
    el.innerHTML = ` · <a class="action" href="${API_BASE}/api/v1/auth/login">Sign in / Register</a>`;
    orgInfo = null;
    signedIn = false; myFavorites = new Set();               // #221
  }
  if (!orgInfo) showOpen = true;       // anonymous: always full open data
  applyOpenVisibility();
  renderList(allSats);
  // Now that the org fleet is known, resolve a private-sat deep link (#org:<name>)
  // that refresh() couldn't while orgInfo was still loading (#176).
  const og = hashOrgSat();
  if (og && orgInfo) {
    const os = orgInfo.sats.find(x => x.satellite === og);
    if (os) selectOrgSat(os);
  }
}
// #223: signing out ends the Keycloak SSO session too, so confirm first — a
// stray click shouldn't drop the session. Global: called from inline onclick.
function signOut(){
  if (!confirm("Sign out of Overwatch?")) return;
  location.href = `${API_BASE}/api/v1/auth/logout`;
}

// #221: star/unstar a satellite (owner-scoped). Global so the inline onclick
// in the list rows can reach it (the app is a classic script).
async function toggleFavorite(norad){
  const on = myFavorites.has(norad);
  try {
    const r = on
      ? await fetch(`${API_BASE}/api/v1/me/satellites/${norad}`, { method: "DELETE" })
      : await fetch(`${API_BASE}/api/v1/me/satellites`, { method: "POST",
          headers: { "Content-Type": "application/json" }, body: JSON.stringify({ norad }) });
    if (r.status === 401){ location.href = `${API_BASE}/api/v1/auth/login`; return; }
    if (!r.ok) return;
    if (on) myFavorites.delete(norad); else myFavorites.add(norad);
    renderList(allSats);
  } catch (e) { /* offline: leave the set unchanged */ }
}

async function deleteOrg(id, name){
  if (!confirm(`Delete organization "${name}"? This purges its private data — irreversible.`)) return;
  const r = await fetch(`${API_BASE}/api/v1/orgs/${id}`, { method: "DELETE" });
  if (r.ok) { alert("Organization deleted."); location.href = `${API_BASE}/api/v1/auth/logout`; }
  else alert((await r.json().catch(()=>({}))).detail || "Could not delete the organization.");
}
async function createOrg(){
  const name = prompt("Organization name:");
  if (!name) return;
  const r = await fetch(`${API_BASE}/api/v1/orgs`, { method: "POST",
    headers: { "content-type": "application/json" }, body: JSON.stringify({ name }) });
  const d = await r.json().catch(() => ({}));
  if (r.ok) { alert("Organization created — signing in again to activate it.");
              location.href = `${API_BASE}/api/v1/auth/login`; }
  else alert(d.detail || "Could not create the organization.");
}
function selectOrgSat(s){
  // Private org satellite: keep the selection in the URL as #org:<name> so the
  // 15s refresh() and hashchange resolve back here instead of reverting to the
  // last open-data #<norad> (#176). Names/fields are org-supplied → escape them.
  activeNorad = null; activeStation = null; activeOrgSat = s.satellite;
  const h = "#org:" + encodeURIComponent(s.satellite);
  if (location.hash !== h) history.replaceState(null, "", h);
  // A private satellite has no SatNOGS receptions and no cached ground track:
  // its telemetry is pushed by the org. Whatever the previously selected
  // open-data satellite left on the globe — the "N ground stations heard this
  // satellite" legend, the orange reception lines, the blue track — would
  // otherwise stay on screen and be read as belonging to THIS satellite.
  setRxLegend("");
  const empty = { type:"FeatureCollection", features: [] };
  for (const src of ["track", "track-arcs", "rx-links", "rx-stations",
                     "rx-endpoints", "rx-hi", "rx-hi-pt"]){
    const o = map.getSource(src);
    if (o) { try { o.setData(empty); } catch (e) {} }
  }
  rxLinkFeatures = []; clearPulse();
  refreshSatHighlight();                 // no open-data satellite is selected now
  document.getElementById("panelHead").innerHTML =
    `${escapeHTML(s.satellite)} — <span class="fbadge fbadge-private" ` +
    `title="Your organization's private satellite">private</span> · ${escapeHTML(orgInfo.name)}`;
  const rows = s.fields.map(f =>
    `${escapeHTML(f.field)} — ${f.points} point${f.points > 1 ? "s" : ""}, last ${age(f.last)}`).join("<br>");
  document.getElementById("panelBody").innerHTML =
    `<div class="empty" style="overflow-y:auto; height:100%">` +
    `<b>Your organization's data — visible to ${escapeHTML(orgInfo.name)} only</b><br><br>` +
    rows + `<br><br><span style="opacity:.7">Read via ` +
    `GET /api/v1/org/telemetry?satellite=${encodeURIComponent(s.satellite)}&field=… ` +
    `— per-organization dashboards arrive with the Grafana integration.</span></div>`;
  renderList(allSats);
}

// Version badge (SaaS + API) — links to the public API landing.
fetch(`${API_BASE}/api/version`).then(r => r.json()).then(v => {
  document.getElementById("ver").innerHTML =
    `<a href="${API_BASE}/api/v1" title="Public API — docs, keys, examples">` +
    `v${v.version} · API ${v.api}</a>`;
}).catch(() => {});

// #256: payment mode must never be ambiguous. The value comes from the API that
// actually performs the checkout — never from a POLAR_ENV copied into this
// container, because two sources drift and a badge that wrongly says "sandbox"
// while real cards are charged is worse than no badge at all.
fetch(`${API_BASE}/api/v1/billing/mode`).then(r => r.json()).then(m => {
  const el = document.getElementById("paymode");
  if (!el) return;
  const env = m.env || m.polar_env;             // polar_env: pre-#269 field name
  if (env === "sandbox"){
    el.className = "paymode sandbox";
    el.textContent = "sandbox payments";
    el.title = "This environment charges TEST cards only (" +
      (m.provider || "billing") + " test mode) — no real money moves.";
  } else if (env === "off"){
    el.className = "paymode off";
    el.textContent = "billing off";
    el.title = "Billing is not connected in this environment.";
  }
}).catch(() => {});

// Basemap: Sentinel-2 cloudless by EOX — real Copernicus imagery, processed
// and served from Europe. The glyph server only feeds the text labels.
const map = new maplibregl.Map({
  container: "map",
  style: {
    version: 8,
    glyphs: "https://demotiles.maplibre.org/font/{fontstack}/{range}.pbf",
    sources: {
      s2: {
        type: "raster", tileSize: 256, maxzoom: 14,
        tiles: ["https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless-2024_3857/default/g/{z}/{y}/{x}.jpg"],
        attribution: "Sentinel-2 cloudless by <a href=\"https://s2maps.eu\">EOX</a> (Copernicus data)"
      }
    },
    layers: [
      { id: "space", type: "background", paint: { "background-color": "#04070d" } },
      { id: "s2", type: "raster", source: "s2" }
    ]
  },
  center: [0, 20], zoom: 1.4, attributionControl: true
});

// Native globe projection (MapLibre GL >= 5) — the right canvas for orbits.
map.on("style.load", () => map.setProjection({ type: "globe" }));

let activeNorad = null;
// Satellite the app opens on when there is no deep link and no favourite.
// Falls back to the freshest-telemetry pick if it ever leaves the fleet.
const DEFAULT_NORAD = 69015;            // FrontierSat
let activeStation = null;          // observer string when in station mode
let activeOrgSat = null;           // private org-satellite name when selected (#176)
let rxLinkFeatures = [];           // current reception lines (for #42 field->line)
let allStations = [];              // 7-day station aggregate (search + list)
async function loadStations(){
  try { allStations = await j("/api/stations"); } catch (e) {}
}
let allSats = [];
let satsByNorad = {};

async function j(u){ const r = await fetch(API_BASE + u); return r.json(); }

// Anonymous first-party usage beacon (no cookies, no ids — counters only).
const ORIGIN = LOCAL ? "local" : MIRROR ? "mirror" : "direct";
function beacon(type, norad){
  fetch(`${API_BASE}/api/event?type=${type}&origin=${ORIGIN}` +
        (norad ? `&norad=${norad}` : ""), { keepalive: true }).catch(() => {});
}
beacon("load");
let searchPinged = null;
document.getElementById("search").addEventListener("input", e => {
  clearTimeout(searchPinged);
  if (e.target.value.trim().length > 1)
    searchPinged = setTimeout(() => beacon("search"), 1500);
});

function age(ts){
  const h = (Date.now() - Date.parse(ts)) / 3.6e6;
  if (h < 1) return `${Math.max(1, Math.round(h * 60))}min ago`;
  if (h < 48) return `${Math.round(h)}h ago`;
  return `${Math.round(h / 24)}d ago`;
}

// Link status per satellite: green = heard <24h, orange = 1-3 days,
// red = silent >3 days (or never heard).
function linkStatus(s){
  if (!s.last_frame) return "r";
  const h = (Date.now() - Date.parse(s.last_frame)) / 3.6e6;
  return h <= 24 ? "g" : h <= 72 ? "o" : "r";
}

// Heard within the last hour -> highlighted as live.
function isLive(s){
  return s.last_frame && (Date.now() - Date.parse(s.last_frame)) < 3.6e6;
}

function renderFleetbar(sats){
  const by = { g:[], o:[], r:[] };
  sats.forEach(s => by[linkStatus(s)].push(s.name));
  const live = sats.filter(isLive);
  const bar = document.getElementById("fleetbar");
  bar.innerHTML =
    `<span><span class="fdot g live"></span>${live.length} live &lt;1h</span>` +
    `<span><span class="fdot g"></span>${by.g.length} nominal</span>` +
    `<span><span class="fdot o"></span>${by.o.length} quiet</span>` +
    `<span><span class="fdot r"></span>${by.r.length} silent</span>`;
  bar.title = (live.length ? "Live (<1h): " + live.map(s => s.name).join(", ") + "\n" : "") +
              (by.o.length ? "Quiet (1-3d): " + by.o.join(", ") + "\n" : "") +
              (by.r.length ? "Silent (>3d): " + by.r.join(", ") : "");
}

document.getElementById("search").addEventListener("input", () => renderList(allSats));

// Satellites render as a GeoJSON circle layer (scales to thousands —
// DOM markers would crawl past a few hundred).

// #: the selected satellite pulses so it is obvious where it is on the globe.
// MapLibre paint properties cannot be CSS-animated, so drive the halo from the
// frame loop — throttled to ~25 fps, and idle when nothing is selected.
let _pulseLast = 0;
function pulseSelected(ts){
  requestAnimationFrame(pulseSelected);
  if (activeNorad == null || !map.getLayer("sat-pulse")) return;
  if (ts - _pulseLast < 40) return;            // ~25 fps is plenty for a beacon
  _pulseLast = ts;
  const t = (ts % 1600) / 1600;                // one breath every 1.6 s
  map.setPaintProperty("sat-pulse", "circle-radius", 12 + 20 * t);
  map.setPaintProperty("sat-pulse", "circle-opacity", 0.38 * (1 - t));
}

// Repaint the fleet so the `sel` flag (and therefore the highlight) follows the
// selection immediately, instead of waiting for the next 15 s refresh.
function refreshSatHighlight(){
  const src = map.getSource("sats");
  if (src && allSats.length) src.setData(satsGeojson(allSats));
}

function satsGeojson(sats){
  return { type:"FeatureCollection", features: sats
    .filter(s => s.lat != null)
    .map(s => ({ type:"Feature",
      geometry:{ type:"Point", coordinates:[s.lon, s.lat] },
      properties:{ norad:s.norad, tel:!!s.has_telemetry, name:s.name,
                   live:isLive(s), sel: s.norad === activeNorad } })) };
}

async function refresh(){
  const sats = await j("/api/satellites");
  allSats = sats;
  satsByNorad = Object.fromEntries(sats.map(s => [s.norad, s]));
  renderList(sats);
  renderFleetbar(sats);
  const src = map.getSource("sats");
  if (src) src.setData(satsGeojson(sats));
  // Deep link: #<norad> in the URL selects that satellite (kept across
  // refreshes and shareable).
  const wanted = hashNorad();
  const wantedStation = hashStation();
  const wantedOrg = hashOrgSat();
  // #oem:<id> overlays an imported CCSDS ephemeris (#208) — an overlay, not a
  // selection: draw once when the id changes, clear when it goes away.
  const wantedOem = hashOem();
  if (wantedOem !== activeOem) { activeOem = wantedOem; wantedOem ? drawOem(wantedOem) : clearOem(); }
  if (wantedStation && wantedStation !== activeStation) {
    selectStation(wantedStation);
  } else if (wantedOrg) {
    // A private-satellite selection (#org:<name>) must persist across refreshes;
    // resolve it back rather than falling through to the open-data default (#176).
    if (wantedOrg !== activeOrgSat && orgInfo) {
      const os = orgInfo.sats.find(x => x.satellite === wantedOrg);
      if (os) selectOrgSat(os);
    }
  } else if (wanted && wanted !== activeNorad && satsByNorad[wanted]) {
    select(satsByNorad[wanted]);
  } else if (activeNorad === null && activeStation === null && activeOrgSat === null && !wanted && !wantedOem && showOpen) {
    // Default view: land on a satellite with the freshest telemetry —
    // inside the hour when the network heard one, otherwise the most
    // recently heard overall. The page never opens on an empty panel.
    // Landing order: the user's own choice first (#221), then the default
    // satellite, then whichever satellite was heard most recently.
    const fav = signedIn ? [...myFavorites].map(n => satsByNorad[n]).find(Boolean) : null;
    const dflt = satsByNorad[DEFAULT_NORAD];
    if (fav) { select(fav, true); }
    else if (dflt) { select(dflt, true); }
    else {
      const heard = sats.filter(s => s.last_frame)
        .sort((a, b) => new Date(b.last_frame) - new Date(a.last_frame));
      if (heard.length) select(heard[0], true);
    }
  }
}

function hashNorad(){
  const m = location.hash.match(/^#(\d+)$/);
  return m ? Number(m[1]) : null;
}
function hashStation(){
  const m = location.hash.match(/^#station:(.+)$/);
  return m ? decodeURIComponent(m[1]) : null;
}
function hashOrgSat(){
  const m = location.hash.match(/^#org:(.+)$/);
  return m ? decodeURIComponent(m[1]) : null;
}
function hashOem(){
  const m = location.hash.match(/^#oem:(.+)$/);
  return m ? decodeURIComponent(m[1]) : null;
}

window.addEventListener("hashchange", () => {
  const n = hashNorad();
  if (n && n !== activeNorad && satsByNorad[n]) select(satsByNorad[n]);
  const st = hashStation();
  if (st && st !== activeStation) selectStation(st);
  const og = hashOrgSat();
  if (og && og !== activeOrgSat && orgInfo) {
    const os = orgInfo.sats.find(x => x.satellite === og);
    if (os) selectOrgSat(os);
  }
});

function renderList(sats){
  const list = document.getElementById("list");
  const q = document.getElementById("search").value.trim().toLowerCase();
  list.innerHTML = "";
  // Private fleet first: the signed-in organization's own satellites.
  if (orgInfo){
    const hdr = document.createElement("div");
    hdr.className = "meta"; hdr.style.padding = "8px 12px 2px";
    hdr.innerHTML = `Your fleet — ${orgInfo.name} (private) · ` +
      `<a href="#" onclick="toggleOpen();return false">${showOpen ? "hide" : "show"} open-data fleet</a>`;
    list.appendChild(hdr);
    if (!orgInfo.sats.length){
      const d = document.createElement("div"); d.className = "empty";
      d.innerHTML = "No data yet — push telemetry with your org token " +
        "(see <a href='/pro.html'>how</a>).";
      list.appendChild(d);
    }
    for (const s of orgInfo.sats){
      const div = document.createElement("div");
      div.className = "sat";
      div.innerHTML = `<div class="row"><span class="name" style="color:var(--accent)">${s.satellite}</span></div>
        <div class="meta">${s.fields.length} field${s.fields.length > 1 ? "s" : ""} · private</div>`;
      div.onclick = () => selectOrgSat(s);
      list.appendChild(div);
    }
  }
  // #255: private mode shows the org fleet PLUS the user's starred open-data
  // satellites — hiding the fleet should not hide what they chose to follow.
  const onlyFavs = orgInfo && !showOpen;
  const favSet = onlyFavs ? new Set(favouriteOpenSats()) : null;
  if (onlyFavs && favSet.size === 0) return;   // nothing starred: org fleet only
  const pool = onlyFavs ? sats.filter(s => favSet.has(s.norad)) : sats;
  const shown = (q ? pool.filter(s =>
    s.name.toLowerCase().includes(q) || String(s.norad).includes(q)) : pool)
    .slice().sort((a, b) =>
      // #221: the user's favourites float to the top when signed in.
      ((myFavorites.has(b.norad) ? 1 : 0) - (myFavorites.has(a.norad) ? 1 : 0)) ||
      (b.has_telemetry - a.has_telemetry) || a.name.localeCompare(b.name));
  for (const s of shown){
    const div = document.createElement("div");
    div.className = "sat" + (s.norad===activeNorad?" active":"");
    const pos = s.lat==null ? "acquiring…"
      : `${s.lat.toFixed(1)}°, ${s.lon.toFixed(1)}° · ${Math.round(s.alt_km)} km`;
    const heard = " · " + (s.last_frame
      ? "heard " + age(s.last_frame) : "no frames yet");
    // #221: a ★ toggle to add/remove this satellite from your set (signed-in
    // only). stopPropagation so starring doesn't also select the satellite.
    const on = myFavorites.has(s.norad);
    const favBtn = signedIn
      ? `<span class="fav${on ? " on" : ""}" title="${on ? "Remove from your satellites" : "Add to your satellites"}" onclick="event.stopPropagation();toggleFavorite(${s.norad})">${on ? "★" : "☆"}</span>`
      : "";
    div.innerHTML =
      `<div class="row"><span class="name"><span class="sdot fdot ${linkStatus(s)}${isLive(s) ? " live" : ""}"></span>${s.country ? flag(s.country) + " " : ""}${s.name}</span>${favBtn}</div>
       <div class="meta">NORAD ${s.norad} · ${pos}${heard}</div>`;
    div.onclick = () => select(s);
    list.appendChild(div);
  }
  // Station results: station-first view for ground-station operators
  // ("does MY station appear?"). Shown when the query matches a callsign.
  const stq = q ? allStations.filter(st =>
    st.observer.toLowerCase().includes(q)) : [];
  if (q && stq.length){
    const hdr = document.createElement("div");
    hdr.className = "meta"; hdr.style.padding = "8px 12px 2px";
    hdr.textContent = "Ground stations (heard the fleet, last 7 days)";
    list.appendChild(hdr);
    for (const st of stq.slice(0, 20)){
      const div = document.createElement("div");
      div.className = "sat" + (st.observer===activeStation?" active":"");
      div.innerHTML =
        `<div class="row"><span class="name" style="color:#f5a623">${st.observer.split("-")[0]}</span></div>
         <div class="meta">station ${st.observer} · ${st.frames} frames · ${st.sats} satellites · last ${age(st.last_rx)}</div>`;
      div.onclick = () => selectStation(st.observer);
      list.appendChild(div);
    }
  }
  if (q && !shown.length && !stq.length){
    const div = document.createElement("div");
    div.className = "empty";
    div.textContent = "No match. Only the tracked fleet (" + allSats.length +
      " satellites) and stations that heard it in the last 7 days are shown.";
    list.appendChild(div);
  }
}

// Station mode: one station's receptions across the whole fleet, reusing the
// orange reception layers. Deep-linkable via #station:OBSERVER.
async function selectStation(observer){
  activeStation = observer; activeNorad = null; activeOrgSat = null;
  const h = "#station:" + encodeURIComponent(observer);
  if (location.hash !== h) history.replaceState(null, "", h);
  const head = document.getElementById("panelHead");
  const body = document.getElementById("panelBody");
  head.innerHTML = `${escapeHTML(observer)} — volunteer ground station (SatNOGS) · ` +
    `<a class="action" href="https://www.qrz.com/db/${encodeURIComponent(baseCall(observer.split("-")[0]))}" ` +
    `target="_blank" rel="noopener" title="Look up / contact the operator">Contact operator ↗</a>`;
  body.innerHTML = `<div class="empty"><span class="dot"></span>Loading receptions…</div>`;
  const recs = await j(`/api/station/${encodeURIComponent(observer)}`);
  if (activeStation !== observer) return;
  // A station has no satellite track; clear it — but guard the sources, which
  // only exist once a satellite has been selected. Landing directly on a
  // #station: deep link (e.g. the outreach links) selects no satellite first,
  // so an unguarded setData here would throw and freeze on "Loading receptions".
  for (const src of ["track", "track-arcs"]){
    const s = map.getSource(src);
    if (s) s.setData({ type:"FeatureCollection", features: [] });
  }
  const bySat = {};
  const links = [], stations = [];
  let lat = null, lon = null;
  for (const r of recs){
    if (r.lat != null){ lat = r.lat; lon = r.lon; }
    const a = bySat[r.norad] || (bySat[r.norad] = { name:r.name, n:0, last:r.ts, norad:r.norad });
    a.n++;
    if (r.sat_lat != null && r.lat != null){
      links.push({ type:"Feature", geometry:{ type:"LineString",
        coordinates:[[r.lon, r.lat], [r.sat_lon, r.sat_lat]] },
        properties:{ observer, ts:new Date(r.ts).getTime(),
                     slat:r.sat_lat.toFixed(1), slon:r.sat_lon.toFixed(1),
                     km: Math.round(haversineKm(r.lat, r.lon, r.sat_lat, r.sat_lon)) } });
    }
  }
  if (lat != null){
    stations.push({ type:"Feature", geometry:{ type:"Point", coordinates:[lon, lat] },
      properties:{ name: observer.split("-")[0], grid: observer.split("-")[1] || "",
                   n: recs.length, first: 0, last: 0 } });
    map.flyTo({ center:[lon, lat], zoom: Math.max(map.getZoom(), 2.6), duration: 1500 });
  }
  map.getSource("rx-links").setData({ type:"FeatureCollection", features: links });
  map.getSource("rx-stations").setData({ type:"FeatureCollection", features: stations });
  map.getSource("rx-endpoints").setData({ type:"FeatureCollection",
    features: links.map(l => ({ type:"Feature",
      geometry:{ type:"Point", coordinates: l.geometry.coordinates[1] }, properties:{} })) });
  rxLinkFeatures = links; clearPulse();
  const sats = Object.values(bySat).sort((a, b) => b.n - a.n);
  setRxLegend(`<b>${observer.split("-")[0]}</b> — this station's receptions` +
    `<div class="sub">${recs.length} frames across ${sats.length} satellites, last 7 days. ` +
    `Each line points to where a satellite was when this station heard it.</div>`);
  // Next passes over THIS station (#217): which satellites will cover it next.
  // Embedded from the shared next-passes board (panel 1), above the receptions.
  const cold = !gfReady; gfReady = true;
  const passesEmbed = `<div class="ggrid">` +
    `<div class="gcell wide tall"><iframe loading="lazy" ` +
    `src="${GRAFANA}/d-solo/next-passes/next-passes?orgId=1&panelId=4&var-station=${encodeURIComponent(observer)}&theme=dark&kiosk"></iframe></div></div>`;
  body.innerHTML = passesEmbed +
    `<div class="empty" style="overflow-y:auto">` +
    `<b>${recs.length} receptions · ${sats.length} satellites (last 7 days)</b><br><br>` +
    sats.map(x => `<a href="#${x.norad}" class="stsat">${escapeHTML(x.name)}</a> — ${x.n} frame${x.n>1?"s":""}, last ${age(x.last)}`).join("<br>") +
    `<br><br><span style="opacity:.7">Tracked fleet only (${allSats.length} satellites), ` +
    `7-day window — a station that heard other satellites will not appear.</span></div>`;
  trackPanelLoading(body, cold);           // count the panel in (#239)
  renderList(allSats);
}

async function select(s, auto = false){
  // auto = default selection on load, not a user action: skip the usage
  // beacon so the "most-inspected satellites" ops panel stays honest.
  if (!auto && s.norad !== activeNorad) beacon("select", s.norad);
  activeStation = null; activeOrgSat = null;
  activeNorad = s.norad;
  refreshSatHighlight();                 // highlight follows the selection now
  if (location.hash !== "#" + s.norad) {
    history.replaceState(null, "", "#" + s.norad);
  }
  // Focus the globe on the satellite (deep links land right on it).
  if (s.lat != null) {
    map.flyTo({ center: [s.lon, s.lat],
                zoom: Math.max(map.getZoom(), 2.4), duration: 1800 });
  }
  document.querySelectorAll(".sat").forEach(e=>e.classList.remove("active"));
  const head = document.getElementById("panelHead");
  const body = document.getElementById("panelBody");
  // Dedicated 3D spacecraft view (#55), opens as a control-room window (#49).
  const view3d = `<a class="action" href="/w/spacecraft?sat=${s.norad}" ` +
    `target="_blank" rel="noopener" title="Dedicated 3D spacecraft view">3D view ↗</a>`;
  // Operator / point of contact — SatNOGS DB lists the mission team + website.
  const opLink = `<a class="action" href="https://db.satnogs.org/satellite/${s.norad}" ` +
    `target="_blank" rel="noopener" title="Operator, website & contact (SatNOGS DB)">Operator ↗</a>`;
  // The mission team's own curated SatNOGS dashboard, when one exists (#88) —
  // discovered per satellite, shown next to our auto-grouped panels below.
  const dashLink = s.satnogs_dashboard
    ? ` · <a class="action" href="${s.satnogs_dashboard}" target="_blank" rel="noopener" ` +
      `title="Mission team's curated SatNOGS Grafana dashboard">Telemetry Dashboard ↗</a>`
    : "";
  const posOnly = s.has_telemetry ? "" :
    ` <span style="color:var(--dim)">— position-only: encrypted or no open downlink; ` +
    `orbit data below, no public health data (shown honestly, not faked)</span>`;
  head.innerHTML = `${escapeHTML(s.name)} — NORAD ${s.norad} · ` +
    `<span class="fbadge fbadge-open" title="Public open-data satellite (SatNOGS / CelesTrak)">open data</span> · ` +
    `${view3d} · ${opLink}${dashLink}${posOnly}`;

  // draw recent ground track + who-heard-it reception network
  await drawTrack(s.norad);
  await drawReceptions(s.norad);

  // Full dashboard in the bottom half. Every satellite has live data there
  // (the altitude panel reads the position cache); health panels fill only
  // for satellites with open, locally-decodable telemetry. (The position-only
  // note is now part of the title head above, so the 3D/Operator links survive.)
  // Two-stage loading: the globe (tiles + flyTo) settles first; the Grafana
  // embeds only start once the map goes idle, so earth never competes with
  // dashboard iframes for bandwidth. A loading note holds the bottom panel.
  body.innerHTML = `<div class="empty"><span class="dot"></span>` +
    `<span id="panelWait">${globeStatusText()}</span></div>`;
  // The panel content must NEVER wait on the globe indefinitely. "idle" fires
  // only once the map has finished rendering, and a slow connection (or a
  // single stalled tile) can keep it busy for tens of seconds — the panel then
  // sat on "Loading dashboards…" forever. Race the idle event against a short
  // timer and take whichever comes first.
  let embedded = false;
  const embed = () => { if (embedded) return; embedded = true; embedDashboards(s); };
  if (map.loaded() && !map.isMoving()) embed();
  else { map.once("idle", embed); setTimeout(embed, 2500); }
  renderList(allSats);
}


// --- Next contacts (#217/#232): a NATIVE panel, not a Grafana embed. The
// control-room question is "when do I next talk to this satellite, from which
// station, and is the pass usable" — so: soonest first, the next one called out,
// imminence colour-coded. Native means it paints immediately (no iframe, no
// Grafana cold start) and the labels stay readable.
function fmtIn(sec){
  if (sec < 60) return "now";
  const m = Math.round(sec / 60);
  if (m < 60) return `in ${m} min`;
  const h = Math.floor(m / 60), r = m % 60;
  return r ? `in ${h}h ${r}m` : `in ${h}h`;
}
function imminence(sec){                    // matches the board's colour bands
  const h = sec / 3600;
  return h < 1 ? "im-red" : h < 6 ? "im-orange" : h < 24 ? "im-yellow" : "";
}
function elClass(deg){                      // is the pass actually usable?
  return deg >= 45 ? "el-high" : deg >= 20 ? "el-mid" : "el-low";
}
function passesPanelHTML(ps){
  if (!ps.length){
    return `<div class="np"><div class="np-head">Next contacts</div>` +
      `<div class="np-empty">No pass over a tracked ground station in the next 24 h.</div></div>`;
  }
  const n = ps[0];
  const rows = ps.slice(0, 12).map(p => {
    const call = String(p.observer).split("-")[0];
    return `<tr><td class="np-when ${imminence(p.in_s)}">${fmtIn(p.in_s)}</td>` +
      `<td class="np-st" title="${escapeHTML(p.observer)}">${escapeHTML(call)}</td>` +
      `<td class="np-el ${elClass(p.max_el_deg)}">${Math.round(p.max_el_deg)}°</td>` +
      `<td class="np-dur">${Math.round(p.dur_s / 60)} min</td></tr>`;
  }).join("");
  return `<div class="np">` +
    `<div class="np-head">Next contact — <b class="${imminence(n.in_s)}">${fmtIn(n.in_s)}</b> ` +
    `via ${escapeHTML(String(n.observer).split("-")[0])} · ${Math.round(n.max_el_deg)}° max · ` +
    `${Math.round(n.dur_s / 60)} min` +
    `<span class="np-sub">${ps.length} pass${ps.length > 1 ? "es" : ""} in the next 24 h</span></div>` +
    `<table class="np-tbl"><tbody>${rows}</tbody></table></div>`;
}



// --- #239: the panel embeds wait for the globe to settle, so the wait must
// name itself. MapLibre streams tiles as `dataloading` / `data` events; count
// the ones in flight and show that, instead of a static "please wait".
let mapPending = 0;
let mapSettled = false;
function globeStatusText(){
  if (mapSettled) return "Globe ready — starting dashboards…";
  return mapPending > 0
    ? `Globe loading — ${mapPending} map tile${mapPending > 1 ? "s" : ""} still arriving…`
    : "Globe rendering…";
}
function paintWaitNote(){
  const el = document.getElementById("panelWait");
  if (el) el.textContent = globeStatusText();
}

// --- #239: panels must never look frozen. Every embedded iframe reports when
// it has actually painted; a pulsing banner counts them in, each pending cell
// shimmers until its own panel arrives, and a panel that never answers SAYS SO
// instead of pulsing forever (which is how an infinite load hid in plain sight).
function trackPanelLoading(container, cold){
  const frames = [...container.querySelectorAll("iframe")];
  if (!frames.length) return;
  const total = frames.length;
  let done = 0;
  const bar = document.createElement("div");
  bar.className = "gfload";
  bar.innerHTML = `<span class="dot"></span>Loading dashboards… <b>0/${total}</b>`;
  container.prepend(bar);
  const tick = () => {
    const b = bar.querySelector("b");
    if (b) b.textContent = `${done}/${total}`;
    if (done >= total) bar.remove();
  };
  for (const f of frames){
    const cell = f.closest(".gcell");
    if (cell) cell.classList.add("gfpending");
    f.addEventListener("load", () => {
      // #105: the first embed of a session paints Grafana's home page, so it is
      // reloaded once. That fires `load` TWICE — only the second means painted,
      // or the counter would reach 100% over blank panels.
      if (cold && !f.dataset.r){
        f.dataset.r = "1";
        const src = f.src;
        setTimeout(() => { f.src = src; }, 700);
        return;
      }
      if (f.dataset.counted) return;
      f.dataset.counted = "1";
      if (cell) cell.classList.remove("gfpending");
      done++; tick();
    });
  }
  setTimeout(() => {                      // still waiting? say it out loud
    if (done < total && bar.isConnected){
      bar.classList.add("gfstuck");
      bar.innerHTML = `<span class="dot"></span>` +
        `<b>${done}/${total}</b> panels loaded — the rest are not answering. ` +
        `<a href="#" onclick="location.reload();return false">reload</a>`;
    }
  }, 20000);
}

async function embedDashboards(s){
  if (s.norad !== activeNorad) return;   // user moved on while the globe settled
  const body = document.getElementById("panelBody");
  const qs = `orgId=1&var-norad=${s.norad}&theme=dark&from=now-${rangeHours}h&to=now`;
  // #105: reload a d-solo iframe once on the first cold embed (what a manual
  // refresh does) so a Grafana-home first paint becomes the real panel.
  const cold = !gfReady; gfReady = true;
  // The next-passes coverage timeline is NOT embedded here: it lives as its own
  // Grafana board (uid next-passes) until the visualisation earns a place in
  // the satellite view (#232).
  // Ask the product API which telemetry fields exist (same 7-day window as
  // the dashboard) and embed only the panels that will actually show data.
  // Both in flight at once — the fields call used to block the passes call, so
  // the panel waited for the slower of the two in series.
  const [fieldsR, passesR] = await Promise.allSettled([
    fetch(`${API_BASE}/api/v1/telemetry/${s.norad}/fields?hours=${rangeHours}`)
      .then(r => r.ok ? r.json() : null),   // [{field, points, last_seen}]
    j(`/api/passes/${s.norad}`),
  ]);
  const all = fieldsR.status === "fulfilled" ? fieldsR.value : null;
  const nextPasses = (passesR.status === "fulfilled" && Array.isArray(passesR.value))
    ? passesR.value : [];
  const passesCell = `<div class="gcell wide auto">${passesPanelHTML(nextPasses)}</div>`;
  if (s.norad !== activeNorad) return;
  if (all === null) {
    body.innerHTML = `<div class="ggrid">` + passesCell +
      `<div class="gcell wide"><iframe src="${GRAFANA}/d/${DASH_UID}/orbit-telemetry?${qs}&kiosk"></iframe></div></div>`;
    trackPanelLoading(body, cold);
    return;
  }
  // A field only justifies a chart if it can draw a line: >= 3 points in
  // the 7-day window. One or two lone dots reads as an empty panel.
  const rich = all.filter(f => f.points >= 3).map(f => f.field);
  const has = re => rich.some(f => re.test(f));
  // Auto-grouped category panels (#88): decoded fields land in meaningful
  // category charts (voltages, temps, currents, power, counters, modes) with no
  // per-sat curation. The panel-12 "Other numeric" catch-all is intentionally
  // NOT embedded here — it dumps every remaining field unlabelled (fine for
  // debugging in Grafana, too noisy for this curated view).
  const COUNT_RE = /count|cnt|seqnum|uptime|reset|boot|reboot|packets|errors/i;
  const POWER_RE = /pwr|power|watt|_w$|charge/i;
  const MODE_RE  = /mode|state|status|flag|enabled|armed|active/i;
  // Panel ids from grafana/dashboards/public/orbit-telemetry.json; each
  // `show` mirrors that panel's SQL field filter.
  // "Latest decoded fields" (panel 4) is now a NATIVE table below, not a
  // Grafana iframe — so a field click can reach the parent map (#42) and each
  // field can be coloured by its source category (#46).
  const panels = [
    // orbit altitude (panel 5) stays in Grafana only — dropped from the app view.
    { id: 14, show: true },            // #86 reception summary (half — pairs with frames/hour)
    { id: 7, show: all.length > 0 },   // frames per hour (half — pairs with reception summary)
    { id: 13, wide: true, show: true },  // #86 ground-station leaderboard — SatNOGS leads with this
    { id: 1, show: rich.includes("battery_v") || has(/volt|vbat|v_bat|bat[a-z_]*_v$|panel_v$/i) },
    { id: 2, show: has(/temp|bat[a-z_]*_t$/i) },
    { id: 3, show: rich.includes("battery_i") || has(/current|curr|_i_|amp/i) },
    { id: 6, show: rich.includes("battery_pct") },
    { id: 10, show: has(POWER_RE) },   // #88 power (W)
    { id: 9, show: has(COUNT_RE) },    // #88 counters & uptime
    { id: 11, show: has(MODE_RE) },    // #88 modes & states (stepped)
    { id: 8, wide: true, show: rich.includes("battery_v") },  // battery vs sunlight fusion
  ];
  const grafanaCells = panels.filter(p => p.show).map(p =>
    `<div class="gcell${p.wide ? " wide" : ""}"><iframe loading="lazy" ` +
    `src="${GRAFANA}/d-solo/${DASH_UID}/orbit-telemetry?${qs}&panelId=${p.id}"></iframe></div>`
  ).join("");
  // The "latest decoded fields" table is no longer shown: the raw field dump was
  // not what the view is for. `all` is still fetched — it decides WHICH charts
  // are worth embedding below.
  body.innerHTML = `<div class="ggrid">` + passesCell + grafanaCells + `</div>`;
  trackPanelLoading(body, cold);           // count the panels in (#239)
  if (all.length) wireFieldRows();
}

// Native "latest decoded fields" table. Fields are grouped by source category
// (#46): canonical health first, then payload telemetry, then transport/
// framing metadata dimmed — signal above noise. Rows are clickable (#42).
function fieldsPanelHTML(fields){
  const rank = { canonical: 0, telemetry: 1, transport: 2 };
  const rows = fields.slice().sort((a, b) =>
    (rank[a.source] - rank[b.source]) || a.field.localeCompare(b.field));
  const rowsHTML = rows.map(f =>
    `<tr class="fld ${f.source}" data-ts="${Date.parse(f.last_seen)}" ` +
    `title="Click to fly to the reception that carried this field">` +
    `<td class="fname">${escapeHTML(f.field)}</td>` +
    `<td class="fval">${fmtValue(f.last_value)}</td>` +
    `<td class="fage">${age(f.last_seen)}</td></tr>`).join("");
  return `<div class="fhdr"><b>Latest decoded fields</b>` +
    `<span class="flegend">` +
    `<span><i class="k canonical"></i>canonical</span>` +
    `<span><i class="k telemetry"></i>telemetry</span>` +
    `<span><i class="k transport"></i>transport</span></span></div>` +
    `<table class="fields"><thead><tr><th>field</th><th style="text-align:right">latest</th>` +
    `<th style="text-align:right">last seen</th></tr></thead><tbody>${rowsHTML}</tbody></table>`;
}

function escapeHTML(s){
  return String(s).replace(/[&<>"]/g, c =>
    ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;" }[c]));
}
// Extract the home callsign for a QRZ lookup from a station label. Stations may
// carry a portable suffix ("UT4UYF/M" → UT4UYF), a foreign prefix ("DL/F1ABC" →
// F1ABC) or a descriptive name ("SA2KNG Omni UHF/VHF" → SA2KNG). QRZ records
// live under the base call, and a slash URL-encodes to %2F which 404s.
function baseCall(label){
  const first = String(label || "").trim().split(/\s+/)[0];  // call is the first token
  // home call is the longest /-separated part (M/P/QRP suffixes & DL/W prefixes are shorter)
  return first.split("/").reduce((a, b) => b.length > a.length ? b : a, "");
}
// ISO-2 country code -> flag emoji via regional-indicator letters (#99).
// No image assets; empty for unknown/multi-country (honest-state).
function flag(cc){
  if (!/^[A-Za-z]{2}$/.test(cc || "")) return "";
  return String.fromCodePoint(...[...cc.toUpperCase()]
    .map(c => 0x1F1E6 + c.charCodeAt(0) - 65));
}
// Great-circle distance (km) between two lat/lon points — #94: how far a
// station's antenna reached to catch a satellite (station -> sub-satellite point).
function haversineKm(lat1, lon1, lat2, lon2){
  const R = 6371, rad = Math.PI / 180;
  const dLat = (lat2 - lat1) * rad, dLon = (lon2 - lon1) * rad;
  const a = Math.sin(dLat / 2) ** 2 +
    Math.cos(lat1 * rad) * Math.cos(lat2 * rad) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.min(1, Math.sqrt(a)));
}
function fmtValue(v){
  if (v === null || v === undefined || v === "") return "—";
  if (typeof v === "number")
    return Number.isInteger(v) ? String(v)
      : String(Number(v.toFixed(3))).replace(/(\.\d*?)0+$/, "$1").replace(/\.$/, "");
  return escapeHTML(v);
}

// #42: clicking a decoded field flies the globe to the reception that carried
// it — the orange line whose timestamp is closest to when the field was last
// decoded — and pulses that line. Matches client-side against the receptions
// already drawn (rxLinkFeatures); no extra API call.
function wireFieldRows(){
  document.querySelectorAll("tr.fld").forEach(tr =>
    tr.addEventListener("click", () => jumpToReception(Number(tr.dataset.ts), tr)));
}
function jumpToReception(fieldTs, tr){
  if (!rxLinkFeatures.length){
    setRxLegend(`<b>No located reception to fly to</b><div class="sub">No station ` +
      `heard this satellite with a known position in the last 7 days.</div>`);
    return;
  }
  let best = null, bestD = Infinity;
  for (const f of rxLinkFeatures){
    const d = Math.abs(f.properties.ts - fieldTs);
    if (d < bestD){ bestD = d; best = f; }
  }
  if (!best) return;
  document.querySelectorAll("tr.fld.sel").forEach(e => e.classList.remove("sel"));
  if (tr) tr.classList.add("sel");
  const end = best.geometry.coordinates[1];   // [sat_lon, sat_lat] — where it was
  map.flyTo({ center: end, zoom: Math.max(map.getZoom(), 2.7), duration: 1400 });
  pulseLink(best);
  const dtMin = Math.round(bestD / 60000);
  const when = dtMin <= 0 ? "at the same moment"
    : `~${dtMin} min ${best.properties.ts < fieldTs ? "before" : "after"}`;
  setRxLegend(`<b>Reception that carried this field</b><div class="sub">` +
    `nearest frame ${when}, decoded by <b>${escapeHTML(best.properties.observer.split("-")[0])}</b> — ` +
    `satellite was over ${best.properties.slat}°, ${best.properties.slon}°.</div>`);
}
let pulseTimer = null, pulseStep = 0;
function clearPulse(){
  clearTimeout(pulseTimer);
  const hi = map.getSource("rx-hi"), pt = map.getSource("rx-hi-pt");
  const empty = { type:"FeatureCollection", features: [] };
  try { if (hi) hi.setData(empty); if (pt) pt.setData(empty); } catch (e) {}
}
function pulseLink(feature){
  const hi = map.getSource("rx-hi"), pt = map.getSource("rx-hi-pt");
  if (!hi || !pt) return;
  hi.setData({ type:"FeatureCollection", features:[feature] });
  pt.setData({ type:"FeatureCollection", features:[{ type:"Feature",
    geometry:{ type:"Point", coordinates: feature.geometry.coordinates[1] }, properties:{} }] });
  clearTimeout(pulseTimer); pulseStep = 0;
  const tick = () => {
    pulseStep++;
    const k = Math.abs(Math.sin(pulseStep / 2));
    try {
      map.setPaintProperty("rx-hi", "line-width", 3 + 2.5 * k);
      map.setPaintProperty("rx-hi-pt", "circle-radius", 5 + 3 * k);
    } catch (e) {}
    if (pulseStep < 12) pulseTimer = setTimeout(tick, 170);
    else pulseTimer = setTimeout(() => {
      try { hi.setData({ type:"FeatureCollection", features:[] });
            pt.setData({ type:"FeatureCollection", features:[] }); } catch (e) {}
    }, 1400);
  };
  tick();
}

// Ground stations that heard the selected satellite (last 7 days), with
// links to where the satellite was when heard (when history covers it).
async function drawReceptions(norad){
  const data = await j(`/api/receptions/${norad}?hours=${rangeHours}`);
  // {points, total, stations} (#175); tolerate the old flat-array shape too.
  const recs = Array.isArray(data) ? data : data.points;
  const st = {};                     // observer -> aggregated station stats
  const links = [];
  for (const r of recs){
    if (r.lat == null) continue;
    const t = new Date(r.ts).getTime();
    let a = st[r.observer];
    if (!a) a = st[r.observer] = { lon:r.lon, lat:r.lat, n:0, first:t, last:t };
    a.n++; if (t < a.first) a.first = t; if (t > a.last) a.last = t;
    if (r.sat_lat != null){
      links.push({ type:"Feature",
        geometry:{ type:"LineString",
          coordinates:[[r.lon, r.lat], [r.sat_lon, r.sat_lat]] },
        properties:{ observer:r.observer, ts:t,
                     slat:r.sat_lat.toFixed(1), slon:r.sat_lon.toFixed(1),
                     km: Math.round(haversineKm(r.lat, r.lon, r.sat_lat, r.sat_lon)) } });
    }
  }
  const stations = Object.entries(st).map(([obs, a]) => ({ type:"Feature",
    geometry:{ type:"Point", coordinates:[a.lon, a.lat] },
    properties:{ name: obs.split("-")[0], grid: obs.split("-")[1] || "",
                 observer: obs, n: a.n, first: a.first, last: a.last } }));
  map.getSource("rx-stations").setData({ type:"FeatureCollection", features: stations });
  map.getSource("rx-links").setData({ type:"FeatureCollection", features: links });
  map.getSource("rx-endpoints").setData({ type:"FeatureCollection",
    features: links.map(l => ({ type:"Feature",
      geometry:{ type:"Point", coordinates: l.geometry.coordinates[1] }, properties:{} })) });
  rxLinkFeatures = links;                 // #42: fields click matches against these
  clearPulse();
  // Uncapped totals for the banner (#175) — recs is capped at 300 for plotting,
  // so the banner must not count it; fall back to it only for the old shape.
  const frames = Array.isArray(data) ? recs.length : data.total;
  const nst = Array.isArray(data) ? stations.length : data.stations;
  setRxLegend(nst
    ? `<b>${nst} ground station${nst>1?"s":""}</b> heard this satellite` +
      `<div class="sub">${frames} reception${frames>1?"s":""}, last 7 days — each orange line ` +
      `points to where the satellite was — on the dashed amber arc of that pass — when a volunteer station decoded a frame. Click one.</div>`
    : "");
}

function setRxLegend(html){
  const el = document.getElementById("rxlegend");
  if (!el) return;
  el.innerHTML = html; el.style.display = html ? "block" : "none";
}

// A polar/inclined ground track crosses the ±180° antimeridian within its
// ~100-min window: consecutive samples jump e.g. +179°->-179°. Drawn as one
// LineString, MapLibre connects them the long way, painting a spurious chord/
// circle across the globe (esp. around the poles, #66). Split into a
// MultiLineString at every seam crossing so no segment spans the wrap.
function splitAntimeridian(coords){
  const segs = [[]];
  for (let i = 0; i < coords.length; i++){
    if (i > 0 && Math.abs(coords[i][0] - coords[i-1][0]) > 180) segs.push([]);
    segs[segs.length - 1].push(coords[i]);
  }
  return segs.filter(s => s.length > 1);
}
// The track now shows only the ARCS where the satellite was heard (one per
// pass), so orange reception lines land on a visible orbit arc instead of
// floating (#70), and 7 days of orbits don't flood the globe. Break the heard
// points into passes on a time gap (>10 min = a different pass), then split
// each pass at the antimeridian (#66) so no arc spans the ±180 seam.
function splitPasses(pts){
  const passes = [[]];
  for (let i = 0; i < pts.length; i++){
    if (i > 0 && (new Date(pts[i].ts) - new Date(pts[i-1].ts)) / 60000 > 10) passes.push([]);
    passes[passes.length - 1].push([pts[i].lon, pts[i].lat]);
  }
  return passes.flatMap(splitAntimeridian);
}

// Popup for a clicked ground-track line (#65): who, when, how long.
function onTrackClick(e){
  // defer to satellite dots / reception layers when they sit under the click
  const over = ["sats-hit", "rx-stations-hit", "rx-links-hit"].filter(l => map.getLayer(l));
  if (over.length && map.queryRenderedFeatures(e.point, { layers: over }).length) return;
  const p = e.features[0].properties;
  const utc = t => new Date(Number(t)).toUTCString().slice(5, -7) + " UTC";
  const mins = (p.first && p.last) ? Math.round((Number(p.last) - Number(p.first)) / 60000) : null;
  new maplibregl.Popup({ maxWidth: "280px" })
    .setLngLat(e.lngLat)
    .setHTML(`<b>Ground track</b><br>` +
      `<b>${p.name}</b> · NORAD ${p.norad}<br>` +
      `${p.points} points over ${mins != null ? mins + " min" : "the recent orbit"}<br>` +
      (p.first ? `${utc(p.first)}<br>→ ${utc(p.last)}<br>` : "") +
      `<span style="opacity:.7">Ground track over the selected window (solid blue) — the satellite is at the leading end. Dashed amber arcs mark heard passes.</span>`)
    .addTo(map);
}

let trackSourceAdded = false;
let oemSourceAdded = false;   // #208: imported-ephemeris overlay (#oem:<id>)
let activeOem = null;
async function drawTrack(norad){
  const s = satsByNorad[norad] || {};
  // Two things at once: the blue ground track over the SELECTED window (#79),
  // ending at the satellite's current position, and the dim amber heard-pass
  // arcs so reception endpoints land on a visible arc (#70).
  const [live, heard] = await Promise.all([
    j(`/api/track/${norad}?hours=${rangeHours}`),
    j(`/api/track/${norad}?heard=1&hours=${rangeHours}`),
  ]);
  const lt = live.map(p => new Date(p.ts).getTime()).filter(Boolean);
  const liveGeo = { type:"Feature",
    properties: { norad, name: s.name || `NORAD ${norad}`, points: live.length,
      first: lt.length ? Math.min(...lt) : 0, last: lt.length ? Math.max(...lt) : 0 },
    geometry:{ type:"MultiLineString", coordinates: splitPasses(live) }};
  const arcsGeo = { type:"Feature",
    geometry:{ type:"MultiLineString", coordinates: splitPasses(heard) }};
  if (!trackSourceAdded){
    map.addSource("track", { type:"geojson", data:liveGeo });
    map.addSource("track-arcs", { type:"geojson", data:arcsGeo });
    // Heard-pass arcs: DASHED AMBER — visually part of the orange reception
    // network (where the satellite was when heard), distinct from the blue
    // orbit in colour, width and style.
    map.addLayer({ id:"track-arcs", type:"line", source:"track-arcs",
      paint:{ "line-color":"#f5a623", "line-width":1.4, "line-opacity":.5,
              "line-dasharray":[2, 2.5] }});
    // Live orbit: SOLID BRIGHT BLUE, ending at the current satellite position —
    // the "where is it now" path.
    map.addLayer({ id:"track", type:"line", source:"track",
      paint:{ "line-color":"#5aa9ff", "line-width":2.4, "line-opacity":.9 }});
    // wide invisible twin so the thin track line is clickable (#65)
    map.addLayer({ id:"track-hit", type:"line", source:"track",
      paint:{ "line-color":"#5aa9ff", "line-width":12, "line-opacity":0.001 }});
    map.on("click", "track-hit", onTrackClick);
    map.on("mouseenter", "track-hit", () => map.getCanvas().style.cursor = "pointer");
    map.on("mouseleave", "track-hit", () => map.getCanvas().style.cursor = "");
    trackSourceAdded = true;
  } else {
    map.getSource("track").setData(liveGeo);
    map.getSource("track-arcs").setData(arcsGeo);
  }
  // Fade and thin the blue track as the window grows, so a 7-day path reads as
  // a faint swath rather than a flood (#79); a short window stays a bright,
  // clean orbit line.
  const tw = rangeHours <= 6 ? 2.4 : rangeHours <= 24 ? 2 : rangeHours <= 72 ? 1.6 : 1.2;
  const to = rangeHours <= 6 ? .9  : rangeHours <= 24 ? .7 : rangeHours <= 72 ? .5  : .35;
  if (map.getLayer("track")){
    map.setPaintProperty("track", "line-width", tw);
    map.setPaintProperty("track", "line-opacity", to);
  }
}

// --- Imported precise ephemeris (#208): plot a user-uploaded CCSDS OEM as a
// distinct CYAN DASHED track — visually apart from the blue SGP4 orbit and the
// amber heard arcs ("this orbit came from a file"). Owner-scoped server-side;
// here it's a pure overlay driven by #oem:<id>. ---
async function drawOem(id){
  const d = await j(`/api/v1/ephemeris/${encodeURIComponent(id)}`);
  const empty = { type:"FeatureCollection", features: [] };
  if (!d || !Array.isArray(d.points) || !d.points.length){
    if (oemSourceAdded) map.getSource("oem-track").setData(empty);
    setOemBanner(d && d.detail ? { error: d.detail } : null);   // 404/401 -> hint
    return;
  }
  const geo = { type:"Feature",
    properties:{ id, label: d.label || d.object_id || "ephemeris" },
    geometry:{ type:"MultiLineString",
      coordinates: splitAntimeridian(d.points.map(p => [p.lon, p.lat])) }};
  if (!oemSourceAdded){
    map.addSource("oem-track", { type:"geojson", data: geo });
    map.addLayer({ id:"oem-track-glow", type:"line", source:"oem-track",
      paint:{ "line-color":"#00e5cc", "line-width":6, "line-opacity":.16 }});
    map.addLayer({ id:"oem-track", type:"line", source:"oem-track",
      paint:{ "line-color":"#00e5cc", "line-width":2.4, "line-opacity":.95,
              "line-dasharray":[3, 2] }});
    oemSourceAdded = true;
  } else {
    map.getSource("oem-track").setData(geo);
  }
  fitOem(d.points);
  setOemBanner(d);
}

function clearOem(){
  if (oemSourceAdded){
    try { map.getSource("oem-track").setData({ type:"FeatureCollection", features: [] }); }
    catch(e){}
  }
  setOemBanner(null);
}

function fitOem(points){
  try {
    const b = new maplibregl.LngLatBounds();
    points.forEach(p => b.extend([p.lon, p.lat]));
    map.fitBounds(b, { padding: 80, maxZoom: 5, duration: 1200 });
  } catch(e){}
}

function setOemBanner(d){
  const el = document.getElementById("oem-banner");
  if (!el) return;
  if (!d){ el.style.display = "none"; el.innerHTML = ""; return; }
  el.style.display = "block";
  const clear = ` <a href="#" onclick="location.hash='';return false">clear</a>`;
  el.innerHTML = d.error
    ? `Imported orbit unavailable — ${escapeHTML(d.error)}` + clear
    : `<b>Imported orbit</b> — ${escapeHTML(d.label || d.object_id || "ephemeris")}` +
      ` · ${d.points.length} pts · CCSDS OEM` + clear;
}

async function importOem(file){
  const status = document.getElementById("oem-status");
  const set = t => { if (status) status.textContent = t; };
  set("reading…");
  let text;
  try { text = await file.text(); } catch(e){ set("read failed"); return; }
  let r;
  try {
    r = await fetch(`${API_BASE}/api/v1/ephemeris`, {
      method:"POST", headers:{ "Content-Type":"application/json" },
      body: JSON.stringify({ oem: text, label: file.name }) });
  } catch(e){ set("upload failed"); return; }
  if (r.status === 401){ location.href = `${API_BASE}/api/v1/auth/login`; return; }
  let d = {};
  try { d = await r.json(); } catch(e){}
  if (!r.ok){ set((d.detail || "invalid OEM").replace(/^Invalid OEM:\s*/, "")); return; }
  set(`✓ ${d.points} points`);
  location.hash = `#oem:${d.id}`;      // triggers drawOem via hashchange -> refresh
}

{  // wire the Import control (elements exist: app.js is the last module in <body>)
  const btn = document.getElementById("oem-import");
  const fin = document.getElementById("oem-file");
  if (btn && fin){
    btn.addEventListener("click", () => fin.click());
    fin.addEventListener("change", () => { if (fin.files[0]) importOem(fin.files[0]); fin.value = ""; });
  }
}

// Globe traffic feeds the waiting note (#239).
map.on("dataloading", () => { mapPending++; paintWaitNote(); });
map.on("data", () => { if (mapPending > 0) mapPending--; paintWaitNote(); });
map.on("idle", () => { mapSettled = true; paintWaitNote(); });

map.on("load", () => {
  map.addSource("sats", { type:"geojson", data:satsGeojson([]) });
  map.addLayer({ id:"sats", type:"circle", source:"sats",
    paint:{
      // the SELECTED satellite is drawn markedly larger so the eye finds it on
      // the globe without hunting (it also gets the pulsing halo below).
      "circle-radius":["case",
        ["get","sel"],  TOUCH ? 15 : 11,
        ["get","live"], TOUCH ? 10 : 6.5,
        TOUCH ? 8 : 4.5],
      "circle-color":["case", ["get","live"], "#4dffa6",
        ["case", ["get","tel"], "#39d98a", "#7c8aa5"]],
      "circle-stroke-width":["case", ["get","sel"], 3.5, ["get","live"], 3, 1.5],
      "circle-stroke-color":["case",
        ["get","sel"], "#ffffff",
        ["get","live"], "rgba(57,217,138,.5)", "rgba(90,169,255,.45)"] }});
    // Pulsing halo under the selected dot — a beacon that says "it is HERE".
    map.addLayer({ id:"sat-pulse", type:"circle", source:"sats",
      filter:["==", ["get","sel"], true],
      paint:{ "circle-radius":12, "circle-color":"#5aa9ff",
              "circle-opacity":0.35, "circle-stroke-width":0 }}, "sats");
    requestAnimationFrame(pulseSelected);
  // Satellite names on the globe ("Open Sans Semibold" is the one font the
  // demotiles glyph server provides). Elevation-true rendering is a future
  // maplibre-engine work item — for now dots sit on the ground track.
  // Invisible enlarged twin of "sats": 4px dots are untappable on phones.
  map.addLayer({ id:"sats-hit", type:"circle", source:"sats",
    paint:{ "circle-radius": TOUCH ? 22 : 16, "circle-color":"#fff", "circle-opacity":0.001 }});
  map.addLayer({ id:"sat-labels", type:"symbol", source:"sats",
    layout:{ "text-field":["get","name"],
      "text-font":["Open Sans Semibold"],
      "text-size":11, "text-anchor":"top", "text-offset":[0, 0.8],
      "text-allow-overlap":false },
    paint:{ "text-color":"#dfe7f5",
      "text-halo-color":"#0a0e17", "text-halo-width":1.2 }});
  // Reception network layers (populated when a satellite is selected).
  const empty = { type:"FeatureCollection", features: [] };
  map.addSource("rx-links", { type:"geojson", data: empty });
  // Soft glow under the lines so the reception network reads at a glance.
  map.addLayer({ id:"rx-links-glow", type:"line", source:"rx-links",
    paint:{ "line-color":"#f5a623", "line-width":4, "line-opacity":0.18, "line-blur":3 }});
  map.addLayer({ id:"rx-links", type:"line", source:"rx-links",
    paint:{ "line-color":"rgba(245,166,35,.8)", "line-width":1.4 }});
  // Endpoint dots: each line ends where the satellite WAS when heard.
  map.addSource("rx-endpoints", { type:"geojson", data: empty });
  map.addLayer({ id:"rx-endpoints", type:"circle", source:"rx-endpoints",
    paint:{ "circle-radius":2.5, "circle-color":"#ffcf6b", "circle-opacity":0.7 }});
  // Invisible wide twin of rx-links: a 1px line is unclickable, this isn't.
  map.addLayer({ id:"rx-links-hit", type:"line", source:"rx-links",
    paint:{ "line-color":"#f5a623", "line-width":12, "line-opacity":0.001 }});
  // #42 highlight: the single reception line a clicked field flew to, pulsed.
  map.addSource("rx-hi", { type:"geojson", data: empty });
  map.addLayer({ id:"rx-hi", type:"line", source:"rx-hi",
    paint:{ "line-color":"#ffd98a", "line-width":3.5, "line-opacity":0.95, "line-blur":0.6 }});
  map.addSource("rx-hi-pt", { type:"geojson", data: empty });
  map.addLayer({ id:"rx-hi-pt", type:"circle", source:"rx-hi-pt",
    paint:{ "circle-radius":6, "circle-color":"#ffd98a", "circle-opacity":0.95,
      "circle-stroke-width":2, "circle-stroke-color":"rgba(255,217,138,.35)" }});
  map.addSource("rx-stations", { type:"geojson", data: empty });
  map.addLayer({ id:"rx-stations", type:"circle", source:"rx-stations",
    paint:{ "circle-radius": TOUCH ? 6.5 : 3.5, "circle-color":"#f5a623",
      "circle-stroke-width":1, "circle-stroke-color":"rgba(10,14,23,.9)" }});
  map.addLayer({ id:"rx-stations-hit", type:"circle", source:"rx-stations",
    paint:{ "circle-radius": TOUCH ? 20 : 14, "circle-color":"#fff", "circle-opacity":0.001 }});
  map.addLayer({ id:"rx-station-labels", type:"symbol", source:"rx-stations",
    layout:{ "text-field":["get","name"], "text-font":["Open Sans Semibold"],
      "text-size":9, "text-anchor":"top", "text-offset":[0, 0.6],
      "text-optional":true },
    paint:{ "text-color":"#f5a623",
      "text-halo-color":"#0a0e17", "text-halo-width":1 }});
  for (const layer of ["sats", "sats-hit"]) {
    map.on("click", layer, e => {
      const s = satsByNorad[e.features[0].properties.norad];
      if (s) select(s);
    });
    map.on("mouseenter", layer, () => map.getCanvas().style.cursor = "pointer");
    map.on("mouseleave", layer, () => map.getCanvas().style.cursor = "");
  }

  // Reception layer popups: a station dot tells who it is and how much it
  // heard; a line tells about that single frame reception.
  const fmtT = t => new Date(+t).toUTCString().slice(5, -7) + " UTC";
  map.on("click", "rx-stations-hit", e => {
    const p = e.features[0].properties;
    new maplibregl.Popup({ maxWidth:"280px" })
      .setLngLat(e.features[0].geometry.coordinates)
      .setHTML(`<b>${p.name}</b> — volunteer ground station (SatNOGS)<br>` +
        (p.grid ? `Maidenhead locator <code>${p.grid}</code><br>` : "") +
        (p.first ? `${p.n} frame${p.n > 1 ? "s" : ""} heard from this satellite (7 d)<br>` +
          `first ${fmtT(p.first)}<br>last ${fmtT(p.last)}<br>` : `${p.n} receptions (7 d)<br>`) +
        (p.observer ? `<a href="#station:${encodeURIComponent(p.observer)}">All receptions by this station →</a><br>` : "") +
        `<a href="https://www.qrz.com/db/${encodeURIComponent(baseCall(p.name))}" target="_blank" rel="noopener">Contact operator ${escapeHTML(p.name)} (QRZ) ↗</a>`)
      .addTo(map);
  });
  map.on("click", "rx-links-hit", e => {
    if (map.queryRenderedFeatures(e.point, { layers:["rx-stations-hit", "sats-hit"] }).length) return;
    const p = e.features[0].properties;
    const sat = satsByNorad[activeNorad];
    new maplibregl.Popup({ maxWidth:"280px" })
      .setLngLat(e.lngLat)
      .setHTML(`<b>Radio reception</b><br>` +
        `<b>${p.observer.split("-")[0]}</b> decoded a frame from ` +
        `<b>${sat ? sat.name : "the satellite"}</b><br>${fmtT(p.ts)}<br>` +
        `satellite was over ${p.slat}°, ${p.slon}° at that moment` +
        (p.km != null ? `<br><b>heard ${Number(p.km).toLocaleString()} km away</b>` : ""))
      .addTo(map);
  });
  for (const l of ["rx-stations-hit", "rx-links-hit"]) {
    map.on("mouseenter", l, () => map.getCanvas().style.cursor = "pointer");
    map.on("mouseleave", l, () => map.getCanvas().style.cursor = "");
  }
  refresh(); setInterval(refresh, 15000);
  loadStations(); setInterval(loadStations, 300000);
  loadAccount();
  setupGutters();
});

// Resizable/collapsible panes (#69): drag a border gutter to resize; drag a
// pane to the edge to hide it, then pull the gutter (which then rests on the
// edge) back in to reveal it. Sizes persist per browser. The globe is WebGL,
// so map.resize() runs after any size change.
function setupGutters(){
  const rs = document.documentElement.style;
  const sw = localStorage.getItem("ovw_sideW"); if (sw) rs.setProperty("--side-w", sw);
  const mh = localStorage.getItem("ovw_mapH");  if (mh) rs.setProperty("--map-h", mh);
  if (map) map.resize();
  dragGutter("gutter-side", e => {
    let w = window.innerWidth - e.clientX;
    w = Math.max(0, Math.min(w, window.innerWidth - 220));
    if (w < 60) w = 0;                          // snap to hidden
    rs.setProperty("--side-w", w + "px");
    localStorage.setItem("ovw_sideW", w + "px");
    if (map) map.resize();
  });
  dragGutter("gutter-map", e => {
    const box = document.getElementById("left").getBoundingClientRect();
    let h = e.clientY - box.top;
    if (h < 60) h = 0; else if (h > box.height - 60) h = box.height;   // snap top/bottom
    rs.setProperty("--map-h", h + "px");
    localStorage.setItem("ovw_mapH", h + "px");
    if (map) map.resize();
  });
}
function dragGutter(id, onMove){
  const el = document.getElementById(id);
  if (!el) return;
  let on = false;
  el.addEventListener("pointerdown", e => {
    on = true; el.classList.add("drag");
    try { el.setPointerCapture(e.pointerId); } catch (_) {}
    document.body.style.userSelect = "none"; e.preventDefault();
  });
  el.addEventListener("pointermove", e => { if (on) onMove(e); });
  const end = e => {
    if (!on) return;
    on = false; el.classList.remove("drag"); document.body.style.userSelect = "";
    try { el.releasePointerCapture(e.pointerId); } catch (_) {}
    if (map) map.resize();
  };
  el.addEventListener("pointerup", end);
  el.addEventListener("pointercancel", end);
}
