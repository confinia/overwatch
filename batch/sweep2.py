"""Sweep v2 — diagnose every candidate: why does it pass/fail?
Wider: alias matching, 14-day window, up to 25 frames tried per satellite."""
import os, json, re, time, requests, importlib, datetime, pkgutil
import satnogsdecoders.decoder as dec

H = {"Authorization": f"Token {os.environ['TOKEN']}", "User-Agent": "orbit-poc/0.1"}
norm = lambda s: re.sub(r'[^a-z0-9]', '', (s or '').lower())

# full alive catalog, matching against name AND names (aliases)
sats, url = [], "https://db.satnogs.org/api/satellites/?format=json&in_orbit=true&status=alive"
while url:
    r = requests.get(url, headers=H, timeout=30); r.raise_for_status()
    d = r.json()
    if isinstance(d, dict):
        sats += d["results"]; url = d.get("next")
    else:
        sats += d; url = None
print(f"catalog: {len(sats)} alive satellites", flush=True)

decoders = {m.name: norm(m.name) for m in pkgutil.iter_modules(dec.__path__)}
BAD = {"cosmo", "stars", "elfin", "real", "enso", "qube", "sr0", "botan"}  # short/generic → false positives

cands = {}
for s in sats:
    hay = [norm(s["name"])] + [norm(a) for a in re.split(r"[,/;]", s.get("names") or "") if a.strip()]
    for mod, dn in decoders.items():
        if mod in BAD:
            continue
        for h in hay:
            if h and (dn == h or (len(dn) >= 5 and (dn in h or h in dn))):
                cands.setdefault(s["norad_cat_id"], (s["name"], s["sat_id"], mod))
                break
print(f"candidates: {len(cands)}", flush=True)

now = datetime.datetime.now(datetime.timezone.utc)
good, stats = [], {"no_frames": 0, "stale": 0, "decode_fail": 0, "http": 0}
for norad, (name, sat_id, mod) in sorted(cands.items()):
    if norad > 90000:
        continue
    try:
        r = requests.get("https://db.satnogs.org/api/telemetry/",
                         params={"sat_id": sat_id, "format": "json"}, headers=H, timeout=20)
        if r.status_code != 200:
            stats["http"] += 1; continue
        d = r.json()
        results = d["results"] if isinstance(d, dict) else d
        if not results:
            stats["no_frames"] += 1; continue
        ts = datetime.datetime.fromisoformat(results[0]["timestamp"].replace("Z", "+00:00"))
        age_h = (now - ts).total_seconds() / 3600
        if age_h > 14 * 24:
            stats["stale"] += 1; continue
        m = importlib.import_module(f"satnogsdecoders.decoder.{mod}")
        cls = getattr(m, mod.capitalize())
        fields, tried = {}, 0
        for fr in results[:25]:
            tried += 1
            try:
                f = {}
                obj = cls.from_bytes(bytes.fromhex(fr["frame"]))
                def flat(o, p="", depth=0):
                    if depth > 4: return
                    for a in dir(o):
                        if a.startswith("_"): continue
                        try: v = getattr(o, a)
                        except Exception: continue
                        if isinstance(v, (int, float)) and not isinstance(v, bool):
                            f[p + a] = v
                        elif hasattr(v, "__class__") and v.__class__.__module__.startswith("satnogsdecoders"):
                            flat(v, p + a + "_", depth + 1)
                flat(obj)
                if len(f) >= 3:
                    fields = f; break
            except Exception:
                pass
        if fields:
            health = [k for k in fields if any(w in k.lower() for w in ("volt", "vbat", "temp", "curr", "batt", "power"))]
            good.append({"norad": norad, "name": name, "sat_id": sat_id, "decoder": mod,
                         "heard_h_ago": round(age_h, 1), "n_fields": len(fields),
                         "health_fields": health[:6]})
            print(f"OK {norad} {name} [{mod}] {age_h:.0f}h ago, {len(fields)} fields, "
                  f"health={health[:3]}", flush=True)
        else:
            stats["decode_fail"] += 1
            print(f"-- decode_fail {norad} {name} [{mod}] (tried {tried} frames, heard {age_h:.0f}h ago)", flush=True)
    except Exception as e:
        stats["http"] += 1
    time.sleep(0.4)

json.dump(good, open("/work/decodable.json", "w"), indent=1)
print("\nGOOD:", len(good), "| reasons for the rest:", stats)
