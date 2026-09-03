"""#432: the public demo-satellite endpoint that lets the web app show a
YAMCS-fed mission in the control room without a tenant key.

Source-invariant guard (like test_passes.py): the endpoint is public
(no auth dependency), keyed off the tenant `demo` flag (never a hardcoded
key), reads the Latitude/Longitude track, and is cacheable.
"""
import os


def _demo_source() -> str:
    src = open(os.path.join(os.path.dirname(__file__), "main.py"),
               encoding="utf-8").read()
    start = src.index("def demo_satellite(")
    return src[start:src.index("\n@app.", start + 1)]


def test_demo_endpoint_is_public_and_flag_scoped():
    fn = _demo_source()
    # public: no auth/login dependency in the signature or body
    sig = fn.split(")")[0]
    assert "Depends" not in sig and "Authorization" not in fn, \
        "the demo endpoint must be public (no auth) — it is safe demo data"
    # scoped by the tenant.demo flag, NOT a hardcoded tenant key/UUID
    assert "FROM tenant WHERE demo" in fn, \
        "the demo tenant must be selected by the `demo` flag"
    import re
    assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}", fn), \
        "no hardcoded tenant UUID in source (the key is a secret)"


def test_demo_endpoint_serves_track_and_is_cacheable():
    fn = _demo_source()
    assert "response: Response" in fn.split(")")[0] + ")", \
        "must take the Response to set cache headers"
    assert '"public, max-age=' in fn, "the demo view should be cacheable"
    assert "'Latitude'" in fn and "'Longitude'" in fn, \
        "the ground track comes from the Latitude/Longitude telemetry"
    assert '"grafana_uid"' in fn, \
        "returns the embeddable Grafana dashboard uid for the app"
