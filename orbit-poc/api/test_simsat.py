"""Guards issue #260: the simulated satellite (devsat) feed.

The model must be plausible enough to teach (orbital rhythm, inertia, resets)
and safe enough to trust (unmistakably simulated, org-scoped, never public).
Pure-model tests need no network; the ingest test drives main.tenant_push the
way the simulator's HTTP client would and checks the rows land org-scoped.
"""
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "simsat"))
import simsat  # noqa: E402


def _run(scenario="nominal", ticks=600, dt=30.0, seed=7, gaps=False):
    sat = simsat.DevSat(seed=seed, scenario=scenario)
    return sat, list(simsat.frames(sat, ticks, dt, 0.0, gaps=gaps))


def test_deterministic_under_a_seed():
    a = [f for _, f in _run(seed=42)[1]]
    b = [f for _, f in _run(seed=42)[1]]
    assert a == b


def test_values_stay_in_plausible_ranges():
    """Five simulated hours: every analog channel within physical bounds."""
    _, out = _run()
    assert len(out) > 500                       # dropouts exist but are rare
    for _, f in out:
        assert 6.8 <= f["battery_v"] <= 8.6, f
        assert -1.0 <= f["battery_a"] <= 1.5, f
        assert 0.0 <= f["panel_a"] <= 1.6, f
        assert -35.0 <= f["temp_ext_c"] <= 40.0, f
        assert f["mode"] in ("NOMINAL", "SAFE")
        assert f["sunlit"] in (0, 1)


def test_orbital_rhythm_shows_in_the_battery():
    """Eclipse discharges, sunlight charges: the battery seen in eclipse must
    run lower than in sunlight, and panels only produce in the sun."""
    _, out = _run(ticks=1200)
    sun = [f["battery_v"] for _, f in out if f["sunlit"]]
    ecl = [f["battery_v"] for _, f in out if not f["sunlit"]]
    assert sun and ecl
    assert min(ecl) < min(sun)
    assert max(f["panel_a"] for _, f in out if not f["sunlit"]) <= 0.2
    assert max(f["panel_a"] for _, f in out if f["sunlit"]) > 0.8


def test_counters_never_go_backwards():
    """uptime is monotonic within a boot and resets only when boot_count
    steps; boot_count itself never decreases."""
    sat = simsat.DevSat(seed=1)
    prev_up, prev_boot = -1, 1
    for _, f in simsat.frames(sat, 5000, 60.0, 0.0, dropout_p=0.0):
        assert f["boot_count"] >= prev_boot
        if f["boot_count"] == prev_boot:
            assert f["uptime_s"] >= prev_up
        prev_up, prev_boot = f["uptime_s"], f["boot_count"]


def test_stuck_thermistor_freezes_exactly_one_channel():
    _, out = _run(scenario="stuck-thermistor", ticks=600)
    late = [f for _, f in out][-100:]
    assert len({f["temp_ext_c"] for f in late}) == 1      # frozen
    assert len({f["temp_battery_c"] for f in late}) > 10  # the others live on


def test_battery_decline_trends_down():
    _, nominal = _run(ticks=4000, dt=60.0)
    _, declining = _run(scenario="battery-decline", ticks=4000, dt=60.0)
    n_last = [f["battery_v"] for _, f in nominal[-200:]]
    d_last = [f["battery_v"] for _, f in declining[-200:]]
    assert sum(d_last) / len(d_last) < sum(n_last) / len(n_last)


def test_silent_subsystem_goes_quiet_not_zero():
    _, out = _run(scenario="silent-subsystem", ticks=600)
    early, late = out[0][1], out[-1][1]
    assert "temp_obc_c" in early
    assert "temp_obc_c" not in late               # a hole, not a zero


def test_contact_gaps_leave_holes_each_orbit():
    _, out = _run(ticks=1200, gaps=True)
    _, full = _run(ticks=1200, gaps=False)
    assert len(out) < len(full) * 0.92


def test_the_name_is_unmistakably_simulated():
    assert simsat._sat_name("MySat").startswith("SIM ")
    assert simsat._sat_name("sim-flatsat-1") == "sim-flatsat-1"
    assert simsat._sat_name("") == "SIM DevSat-1"


def test_production_is_refused_by_default():
    src = open(os.path.join(os.path.dirname(__file__), "..", "simsat",
                            "simsat.py"), encoding="utf-8").read()
    assert "SIM_ALLOW_PROD" in src
    assert "refusing the production API" in src


def test_points_serialize_to_the_push_contract():
    """The wire shape must be exactly what TenantPush validates."""
    import main
    sat = simsat.DevSat(seed=3)
    sat.step(30)
    pts = simsat.to_points(1755475200.0, sat.sample())
    body = main.TenantPush(satellite="SIM DevSat-1", points=pts)
    assert len(body.points) >= 9
    assert body.points[0].ts.endswith("Z")


def test_simulated_frames_land_org_scoped_and_never_public():
    """Rule-13 core: push N simulated frames through the real ingest path into
    a test tenant — they land in tenant_telemetry under that tenant only, and
    the public telemetry/satellite tables gain nothing."""
    import psycopg2
    import psycopg2.pool
    import main
    dsn = os.environ.get("DB_DSN")
    if not dsn:
        import pytest
        pytest.skip("no database in this environment")
    key = str(uuid.uuid4())
    # tenant_push opens main.cursor(), which draws from the app pool the
    # lifespan normally creates — give the module one for the test's duration
    main.pool = psycopg2.pool.SimpleConnectionPool(1, 2, dsn)
    conn = psycopg2.connect(dsn)
    init = open(os.path.join(os.path.dirname(__file__), "..", "db", "init.sql"),
                encoding="utf-8").read()
    with conn, conn.cursor() as cur:
        cur.execute(init)                       # the public tables
        cur.execute(main.KEYS_SQL)              # tenants, tokens, orgs
        cur.execute("INSERT INTO tenant (key, name, email) VALUES (%s::uuid, %s, %s)",
                    (key, "simsat-test", "simsat@test.invalid"))
    conn.commit()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM telemetry")
            pub_before = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM satellite")
            sat_before = cur.fetchone()[0]
        sat = simsat.DevSat(seed=9)
        pushed = 0
        for ts, frame in simsat.frames(sat, 20, 30.0, 1755475200.0, dropout_p=0.0):
            body = main.TenantPush(satellite="SIM DevSat-1",
                                   points=simsat.to_points(ts, frame))
            r = main.tenant_push(key, body)
            pushed += r["accepted"]
        assert pushed >= 180
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM tenant_telemetry "
                        "WHERE tenant = %s::uuid", (key,))
            assert cur.fetchone()[0] == pushed
            cur.execute("SELECT count(*) FROM tenant_telemetry "
                        "WHERE tenant = %s::uuid AND satellite NOT LIKE 'SIM%%'",
                        (key,))
            assert cur.fetchone()[0] == 0          # everything marked simulated
            cur.execute("SELECT count(*) FROM telemetry")
            assert cur.fetchone()[0] == pub_before  # public tables untouched
            cur.execute("SELECT count(*) FROM satellite")
            assert cur.fetchone()[0] == sat_before
    finally:
        with conn, conn.cursor() as cur:
            cur.execute("DELETE FROM tenant_telemetry WHERE tenant = %s::uuid", (key,))
            cur.execute("DELETE FROM tenant WHERE key = %s::uuid", (key,))
        conn.close()
        if main.pool:
            main.pool.closeall()
            main.pool = None
