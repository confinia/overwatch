"""Full sweep: every alive catalog satellite whose name/alias matches one of
the 161 local decoders -> does it have SatNOGS frames < 7 days old that decode?
Paced for SatNOGS throttling (5s between satellites, 60s timeouts, 429 backoff).
Writes /work/sweep_full.json; validated entries go into satellites.py."""
import os, json, re, time, requests, importlib, datetime, pkgutil
import satnogsdecoders.decoder as dec

H = {"Authorization": f"Token {os.environ['TOKEN']}", "User-Agent": "orbit-poc/0.1"}
norm = lambda s: re.sub(r'[^a-z0-9]', '', (s or '').lower())
KNOWN = {25544, 40967, 43017, 43137, 60237, 57175, 55104, 60246, 40931}


def get(url, **params):
    for _ in range(4):
        try:
            r = requests.get(url, params=params, headers=H, timeout=60)
        except requests.exceptions.RequestException:
            time.sleep(10)
            continue
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", 20)) + 2)
            continue
        r.raise_for_status()
        return r.json()
    return None


def flatten(obj, prefix="", depth=0, out=None):
    if out is None:
        out = {}
    if depth > 4:
        return out
    for a in dir(obj):
        if a.startswith("_"):
            continue
        try:
            v = getattr(obj, a)
        except Exception:
            continue
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[prefix + a] = v
        elif hasattr(v, "__class__") and v.__class__.__module__.startswith("satnogsdecoders"):
            flatten(v, prefix + a + "_", depth + 1, out)
    return out


sats, url = [], "https://db.satnogs.org/api/satellites/?format=json&in_orbit=true&status=alive"
while url:
    d = get(url)
    if d is None:
        break
    if isinstance(d, dict):
        sats += d["results"]; url = d.get("next")
    else:
        sats += d; url = None
print(f"catalog: {len(sats)}", flush=True)

decoders = {m.name: norm(m.name) for m in pkgutil.iter_modules(dec.__path__)}
BAD = {"cosmo", "stars", "elfin", "real", "enso", "qube", "sr0", "botan", "iss",
       "fox", "cute", "canvas", "qbee", "spoc", "mxl", "ksu"}  # generic/covered

cands = {}
for s in sats:
    hay = [norm(s["name"])] + [norm(a) for a in re.split(r"[,/;]", s.get("names") or "") if a.strip()]
    for mod, dn in decoders.items():
        if mod in BAD:
            continue
        if any(h and (dn == h or (len(dn) >= 5 and (dn in h or h in dn))) for h in hay):
            cands.setdefault(s["norad_cat_id"], (s["name"], s["sat_id"], mod))
print(f"candidates: {len(cands)}", flush=True)

now = datetime.datetime.now(datetime.timezone.utc)
good = []
for norad, (name, sat_id, mod) in sorted(cands.items()):
    if norad in KNOWN or norad > 90000:
        continue
    try:
        d = get("https://db.satnogs.org/api/telemetry/", sat_id=sat_id, format="json")
        results = (d["results"] if isinstance(d, dict) else d) if d else []
        if not results:  # sat_id sometimes stale — retry by norad
            d = get("https://db.satnogs.org/api/telemetry/", norad_cat_id=norad, format="json")
            results = (d["results"] if isinstance(d, dict) else d) if d else []
        if not results:
            time.sleep(5)
            continue
        ages = []
        for fr in results:
            try:
                t = datetime.datetime.fromisoformat(fr["timestamp"].replace("Z", "+00:00"))
                if t <= now + datetime.timedelta(hours=1):
                    ages.append((now - t).total_seconds() / 3600)
            except Exception:
                pass
        if not ages or min(ages) > 168:
            time.sleep(5)
            continue
        m = importlib.import_module(f"satnogsdecoders.decoder.{mod}")
        cls = getattr(m, mod.capitalize())
        fields = {}
        for fr in results[:50]:
            try:
                f = flatten(cls.from_bytes(bytes.fromhex(fr["frame"])))
                if len(f) >= 3:
                    fields = f
                    break
            except Exception:
                pass
        if fields:
            health = [k for k in fields if any(w in k.lower() for w in
                      ("volt", "vbat", "temp", "curr", "batt", "power"))]
            good.append({"norad": norad, "name": name, "decoder": mod,
                         "heard_h_ago": round(min(ages), 1),
                         "n_fields": len(fields), "health_fields": health[:6]})
            print(f"OK {norad} {name} [{mod}] {min(ages):.0f}h, {len(fields)} fields, "
                  f"health={health[:3]}", flush=True)
    except Exception as e:
        print(f"-- {norad} {name}: {type(e).__name__}", flush=True)
    time.sleep(5)

json.dump(good, open("/work/sweep_full.json", "w"), indent=1)
print(f"\nFINAL: {len(good)} new decodable satellites heard < 7 days", flush=True)
