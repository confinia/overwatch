"""
Imaging scenes from open STAC catalogs (#457).

Planet publishes its Crisis Response releases as a public STAC 1.1 catalog
(no auth, CC-BY-NC-4.0): a root, one catalog per disaster, pre/post-event
sub-catalogs or collections, and one item per scene with the footprint, the
capture and publication times, the platform that took it and COG assets.

What it adds to a control room that otherwise only sees telemetry: the
mission OUTCOME. For an event, the footprints drawn on the globe at their
capture time, linked to the satellite that took them (Planet's operator feed,
#456, names every SkySat, Pelican and Flock), and one KPI no beacon carries:
capture-to-publication lag.

Metadata only. Item id, footprint, times, platform, asset URLs; never the
imagery bytes, which stay at the publisher. And the licence decides where it
shows: the open view only, never a paid surface.

Budget: the whole tree is ~50 catalog/collection documents plus one JSON per
scene (~340 today). Items already stored are never fetched again, and a
collection whose imagery is older than SCENE_ACTIVE_DAYS is not re-walked
once it has scenes, so the steady state is ~50 small GETs per cycle, once a
day. Same shape as the other sources: `get` injectable, no network in tests.
"""
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

log = logging.getLogger("ingest.scenes")

# "label=url,label=url" like OPERATOR_TLE_FEEDS; empty = no scene layer at all
# (the self-host default: nothing leaves the host that the operator did not ask for).
STAC_CATALOGS = os.environ.get("STAC_CATALOGS", "")
SCENE_INTERVAL = int(os.environ.get("SCENE_INTERVAL", 86400))
SCENE_ACTIVE_DAYS = int(os.environ.get("SCENE_ACTIVE_DAYS", 30))
SCENE_TIMEOUT = float(os.environ.get("SCENE_TIMEOUT", 30))
MAX_DOCS = int(os.environ.get("SCENE_MAX_DOCS", 2000))   # one walk's request ceiling


def catalogs(spec=None):
    out = []
    for item in (spec if spec is not None else STAC_CATALOGS).split(","):
        item = item.strip()
        if not item:
            continue
        label, sep, url = item.partition("=")
        label, url = label.strip().lower(), url.strip()
        if not sep or not label or not url.startswith("http"):
            log.warning("STAC_CATALOGS entry ignored: %r (want label=url)", item)
            continue
        out.append((label, url))
    return out


def _ts(s):
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _links(doc, base, rel):
    return [urljoin(base, l["href"]) for l in doc.get("links", [])
            if l.get("rel") == rel and l.get("href")]


def _phase(url):
    """pre-event / post-event from the path, the way Planet lays the tree out;
    None when the tree is flat."""
    for p in ("pre-event", "post-event"):
        if f"/{p}/" in url:
            return p
    return None


def platform_token(constellation, platform):
    """(name prefix, token) to find the satellite in Planet's operator feed,
    whose names read "SKYSAT C1 S3", "PELICAN 2 3009 3009", "FLOCK 4Q 16 24E5".
    STAC says platform "SSC1", "3009", "24e5"."""
    if not platform:
        return None, None
    c = (constellation or "").lower()
    p = platform.upper()
    if c == "skysat" or p.startswith("SSC"):
        return "SKYSAT", ("C" + p[3:]) if p.startswith("SSC") else p
    if c == "pelican":
        return "PELICAN", p
    if c == "planetscope":
        return "FLOCK", p
    return None, p


def resolve_norad(cur, constellation, platform):
    prefix, token = platform_token(constellation, platform)
    if not token:
        return None
    cur.execute("""SELECT norad FROM satellite
                    WHERE note LIKE 'Operator feed%%'
                      AND (%s IS NULL OR upper(name) LIKE %s || ' %%')
                      AND %s = ANY(string_to_array(upper(name), ' '))
                    ORDER BY norad LIMIT 1""", (prefix, prefix, token))
    row = cur.fetchone()
    return row[0] if row else None


def scene_row(item, event, collection, phase, url):
    """One STAC item -> the scene tuple, or None when it lacks what a scene
    needs (a footprint and a capture time)."""
    p = item.get("properties") or {}
    geom = item.get("geometry")
    captured = _ts(p.get("datetime"))
    if not geom or not captured or not item.get("id"):
        return None
    assets = item.get("assets") or {}

    def href(k):
        a = assets.get(k) or {}
        return urljoin(url, a["href"]) if a.get("href") else None
    return (item["id"], event, collection, phase, p.get("constellation"),
            p.get("platform"), captured, _ts(p.get("published")),
            p.get("gsd"), p.get("eo:cloud_cover"), json.dumps(geom),
            href("thumbnail"), href("visual"), url)


class _Budget(Exception):
    pass


def walk(get, root_url, known_ids, has_scenes, now=None):
    """Depth-first over `child` links; Collections yield their `item` links.
    Returns (events, [(item_url, event, collection, phase)]) — the item URLs
    still to fetch. `known_ids` skips stored scenes by id (the id is the
    item URL's last path segment, so no fetch is needed to know); a cold
    collection with scenes already stored is not re-walked. MAX_DOCS caps
    the requests of one walk: past it the walk stops and returns what it
    has, so a runaway catalog costs a bounded number of GETs."""
    now = now or datetime.now(timezone.utc)
    budget = [MAX_DOCS]
    events, todo = [], []

    def fetch(url):
        if budget[0] <= 0:
            raise _Budget()
        budget[0] -= 1
        r = get(url)
        r.raise_for_status()
        return r.json()

    root = fetch(root_url)
    source_title = root.get("title") or root.get("id") or root_url
    for ev_url in _links(root, root_url, "child"):
        try:
            ev = fetch(ev_url)
        except _Budget:
            log.warning("STAC walk stopped: %d documents budget spent", MAX_DOCS)
            return events, todo
        except Exception as e:
            log.warning("event catalog %s failed: %s", ev_url, e)
            continue
        slug = ev.get("id") or ev_url.rstrip("/").split("/")[-2]
        events.append((slug, source_title, ev.get("title") or slug,
                       ev.get("description"), ev_url))
        stack = [ev_url]
        seen = set()
        while stack:
            url = stack.pop()
            if url in seen:
                continue
            seen.add(url)
            doc = ev if url == ev_url else None
            if doc is None:
                try:
                    doc = fetch(url)
                except _Budget:
                    log.warning("STAC walk stopped: %d documents budget spent", MAX_DOCS)
                    return events, todo
                except Exception as e:
                    log.warning("%s failed: %s", url, e)
                    continue
            if doc.get("type") == "Collection":
                coll = doc.get("id") or url
                end = ((doc.get("extent") or {}).get("temporal") or {}).get("interval")
                end_ts = _ts(end[0][1]) if end and end[0] and end[0][1] else None
                cold = end_ts is not None and now - end_ts > timedelta(days=SCENE_ACTIVE_DAYS)
                if cold and has_scenes(coll):
                    continue
                for it in _links(doc, url, "item"):
                    item_id = it.rstrip("/").split("/")[-1].rsplit(".", 1)[0]
                    if item_id not in known_ids:
                        todo.append((it, slug, coll, _phase(it)))
            stack.extend(_links(doc, url, "child"))
    return events, todo


def fetch_scenes(db, get, headers=None, timeout=None):
    """One cycle over every configured catalog: new scenes stored, events
    refreshed. Returns the number of scenes added."""
    added = 0
    for label, root in catalogs():
        try:
            with db() as conn, conn.cursor() as cur:
                cur.execute("SELECT id FROM scene")
                known = {r[0] for r in cur.fetchall()}
                cur.execute("SELECT collection, count(*) FROM scene GROUP BY 1")
                counts = dict(cur.fetchall())
            g = (lambda u: get(label, u, headers=headers,
                               timeout=timeout or SCENE_TIMEOUT))
            events, todo = walk(g, root, known, lambda c: counts.get(c, 0) > 0)
            with db() as conn, conn.cursor() as cur:
                for slug, source, title, desc, url in events:
                    cur.execute("""INSERT INTO event (slug, source, title, description, url)
                                   VALUES (%s,%s,%s,%s,%s)
                                   ON CONFLICT (slug) DO UPDATE SET title = EXCLUDED.title,
                                     description = EXCLUDED.description,
                                     updated_at = now()""",
                                (slug, source, title, desc, url))
                conn.commit()
                for item_url, slug, coll, phase in todo:
                    try:
                        r = g(item_url)
                        r.raise_for_status()
                        row = scene_row(r.json(), slug, coll, phase, item_url)
                    except Exception as e:
                        log.warning("scene %s failed: %s", item_url, e)
                        continue
                    if not row:
                        continue
                    norad = resolve_norad(cur, row[4], row[5])
                    cur.execute("""INSERT INTO scene (id, event, collection, phase,
                                     constellation, platform, captured, published, gsd,
                                     cloud_cover, footprint, thumbnail, visual, url, norad)
                                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                                   ON CONFLICT (id) DO NOTHING""", row + (norad,))
                    conn.commit()
                    added += 1
            log.info("Scenes: '%s' -> %d events, %d new scenes", label, len(events), len(todo))
        except Exception as e:
            log.warning("STAC catalog '%s' failed: %s", label, e)
    return added
