"""Find showcase candidates: satellites recently heard on SatNOGS whose raw
frames we can decode LOCALLY with satnogs-decoders. Runs in a container."""
import os, json, time, requests, importlib, datetime

H = {"Authorization": f"Token {os.environ['TOKEN']}", "User-Agent": "orbit-poc/0.1"}
matches = json.load(open("/work/matches.json"))


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
good, seen = [], set()
for norad, name, sat_id, mod in matches:
    if norad in seen or norad > 90000:  # skip temporary ids
        continue
    try:
        r = requests.get("https://db.satnogs.org/api/telemetry/",
                         params={"sat_id": sat_id, "format": "json"},
                         headers=H, timeout=20)
        if r.status_code != 200:
            continue
        data = r.json()
        results = data["results"] if isinstance(data, dict) else data
        if not results:
            continue
        ts = datetime.datetime.fromisoformat(results[0]["timestamp"].replace("Z", "+00:00"))
        age_h = (now - ts).total_seconds() / 3600
        if age_h > 72:
            continue
        m = importlib.import_module(f"satnogsdecoders.decoder.{mod}")
        cls = getattr(m, mod.capitalize())
        fields = {}
        for fr in results[:5]:
            try:
                fields = flatten(cls.from_bytes(bytes.fromhex(fr["frame"])))
                if fields:
                    break
            except Exception:
                pass
        if fields:
            health = [k for k in fields
                      if any(w in k.lower() for w in ("volt", "vbat", "temp", "curr", "batt"))]
            good.append({"norad": norad, "name": name, "sat_id": sat_id,
                         "decoder": mod, "heard_h_ago": round(age_h, 1),
                         "n_fields": len(fields), "health_fields": health[:6]})
            seen.add(norad)
            print(f"OK {norad} {name} [{mod}] heard {age_h:.1f}h ago, "
                  f"{len(fields)} fields, health: {health[:4]}", flush=True)
    except Exception:
        pass
    time.sleep(0.5)

json.dump(good, open("/work/decodable.json", "w"), indent=1)
print("\ntotal decodable + heard <72h:", len(good))
