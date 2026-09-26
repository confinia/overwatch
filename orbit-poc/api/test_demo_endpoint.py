"""#432/#436: the public demo-satellite endpoint that lets the web app show a
YAMCS-fed mission in the control room without a tenant key, and the pieces that
make that demo actually showable: the position telemetry it draws, and an
address that can be linked to.

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


# --- #436: the demo has to be reachable, and there has to be something to see


def _read(*parts):
    return open(os.path.join(os.path.dirname(__file__), "..", *parts),
                encoding="utf-8").read()


def test_the_demo_subscribes_to_where_the_satellite_is():
    """The endpoint builds its ground track from Latitude/Longitude, and the
    control room draws that track. The demo asked YAMCS for battery voltages
    only, so the public demo was a table of numbers over an empty globe — the
    quickstart publishes the position all along."""
    compose = _read("bridge", "yamcs", "demo", "docker-compose.yml")
    params = compose.split("YAMCS_PARAMETERS:", 1)[1].split("SATELLITE:", 1)[0]
    for p in ("/myproject/Latitude", "/myproject/Longitude"):
        assert p in params, f"the demo must subscribe to {p}"


def test_the_demo_has_an_address_a_link_can_point_at():
    """`#demo` is a fragment: it never reaches a server, so it cannot be
    proxied to, routed to, or relied on in an announcement. /demo serves the
    same page and the app opens the demo from the path."""
    assert '@app.get("/demo")' in _read("web", "app.py")
    js = _read("web", "static", "app.js")
    assert "function wantsDemo(" in js, \
        "a declaration, not a const: it is called from a handler defined above"
    assert '"/demo"' in js and '"#demo"' in js, \
        "both the path and the fragment must open the demo"
