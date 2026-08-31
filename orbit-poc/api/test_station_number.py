"""Guards issue #417: a station is findable by the number SatNOGS gave it.

PE2BZ: "I cannot enter the station number I want to monitor." Operators know
their station by its Network number, because that is what the URL of their
own station page shows. Every frame carries station_id and we were dropping
it at ingest.
"""
import os

HERE = os.path.dirname(__file__)


def _read(*parts):
    return open(os.path.join(HERE, *parts), encoding="utf-8").read()


def test_ingest_keeps_the_station_number():
    src = _read("..", "ingest", "ingest.py")
    assert 'f.get("station_id")' in src, "the frame carries it; keep it"
    assert "source, station_id" in src, "reception must store it"


def test_schema_carries_it_on_both_paths():
    assert "station_id   INTEGER" in _read("..", "db", "init.sql")
    src = _read("main.py")
    assert "ADD COLUMN IF NOT EXISTS station_id INTEGER" in src, \
        "existing databases must migrate at api startup"
    assert "reception_station_id_idx" in src, \
        "number lookups need the partial index"


def test_the_stations_list_exposes_the_number():
    src = _read("main.py")
    assert '"station_id": sid' in src


def test_a_bare_number_resolves_to_its_station_everywhere():
    """One resolver shared by the three callsign endpoints, so the number
    works for health, watch and pass alike — not just wherever someone
    remembered to add the branch."""
    src = _read("main.py")
    assert "def _resolve_station(" in src
    assert src.count("_resolve_station(cur, callsign)") == 3, \
        "all three endpoints must share the resolver"
    fn = src[src.index("def _resolve_station("):]
    fn = fn[:fn.index("\n\n\n")]
    assert "callsign.isdigit()" in fn and "station_id = %s" in fn
