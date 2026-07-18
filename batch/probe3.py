"""Probe v3 — resolve by norad_cat_id (the search endpoint is what times out),
decode locally, gate on frames heard in the last 7 days."""
import os, json, time, requests, importlib, datetime

H = {"Authorization": f"Token {os.environ['TOKEN']}", "User-Agent": "orbit-poc/0.1"}

CANDIDATES = [
    ("GREENCUBE (IO-117)", [53106, 52759], "greencube"),
    ("VERONIKA", [58261], "veronika"),
    ("Lucky-7", [44406], "lucky7"),
    ("BDSAT-2", [55098], "bdsat2"),
    ("GRBAlpha", [47959], "grbalpha"),
    ("GRBBeta", [60237], "grbbeta"),
    ("PLANETUM-1", [52738], "planetum1"),
    ("VZLUSAT-2", [51085], "vzlusat2"),
    ("Delfi-PQ", [51074], "delfipq"),
    ("SALSAT", [46495], "salsat"),
    ("EIRSAT-1", [58472], "eirsat1"),
    ("OreSat0", [51087], "oresat0"),
    ("CSIM-FD", [43793], "csim"),
    ("StratoSat-TK1", [57167], "stratosattk1"),
    ("CUBEBEL-2", [57175], "cubebel2"),
    ("SharjahSat-1", [55104], "sharjahsat1"),
    ("CatSat", [60246], "catsat"),
    ("CAS-4A", [42761], "cas4"),
    ("CAS-4B", [42759], "cas4"),
    ("IO-86", [40931], "io86"),
    ("MRC-100", [56993], "mrc100"),
    ("HADES-R", [59115], "hadesr"),
    ("Geoscan-Edelveis", [53385], "geoscan"),
    ("ISS", [25544], "iss"),
]


def get(url, **params):
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, headers=H, timeout=60)
        except requests.exceptions.Timeout:
            print("   timeout, retrying", flush=True)
            continue
        if r.status_code == 429:
            wait = int(r.headers.get("Retry-After", 15)) + 1
            print(f"   429 — waiting {wait}s", flush=True)
            time.sleep(wait)
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


now = datetime.datetime.now(datetime.timezone.utc)
good = []
for name, norads, mod in CANDIDATES:
    try:
        sat = None
        for norad in norads:
            sats = get("https://db.satnogs.org/api/satellites/", norad_cat_id=norad, format="json")
            if sats:
                sat = (sats["results"] if isinstance(sats, dict) else sats)
                sat = sat[0] if sat else None
            if sat:
                break
        if not sat:
            print(f"-- {name}: not in catalog", flush=True)
            time.sleep(3)
            continue
        d = get("https://db.satnogs.org/api/telemetry/", sat_id=sat["sat_id"], format="json")
        results = (d["results"] if isinstance(d, dict) else d) if d else []
        if not results:
            print(f"-- {name}: no frames", flush=True)
            time.sleep(3)
            continue
        ages = []
        for fr in results:
            try:
                t = datetime.datetime.fromisoformat(fr["timestamp"].replace("Z", "+00:00"))
                if t <= now + datetime.timedelta(hours=1):
                    ages.append((now - t).total_seconds() / 3600)
            except Exception:
                pass
        age_h = min(ages) if ages else 1e9
        m = importlib.import_module(f"satnogsdecoders.decoder.{mod}")
        cls = getattr(m, mod.capitalize())
        fields = {}
        for fr in results[:25]:
            try:
                f = flatten(cls.from_bytes(bytes.fromhex(fr["frame"])))
                if len(f) >= 3:
                    fields = f
                    break
            except Exception:
                pass
        health = [k for k in fields if any(w in k.lower()
                  for w in ("volt", "vbat", "temp", "curr", "batt", "power"))]
        ok = fields and age_h < 168
        print(f"{'OK ' if ok else '-- '}{sat['norad_cat_id']} {sat['name']} [{mod}] "
              f"freshest {age_h:.0f}h, {len(fields)} fields, health={health[:4]}", flush=True)
        if ok:
            good.append({"norad": sat["norad_cat_id"], "name": sat["name"],
                         "sat_id": sat["sat_id"], "decoder": mod,
                         "heard_h_ago": round(age_h, 1), "n_fields": len(fields),
                         "health_fields": health[:8]})
    except Exception as e:
        print(f"-- {name}: error {type(e).__name__} {e}", flush=True)
    time.sleep(3)

json.dump(good, open("/work/curated.json", "w"), indent=1)
print("\nFINAL:", len(good), "satellites decodable with frames < 7 days old")
