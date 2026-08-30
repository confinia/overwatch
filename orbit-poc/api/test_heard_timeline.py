"""Guards issue #403: the station board shows what was HEARD, not only what
passed over. Passes are the opportunity; receptions are the result; side by
side they are the "was it me, or was it quiet" picture."""
import json
import os

BOARD = os.path.join(os.path.dirname(__file__), "..", "grafana", "dashboards",
                     "public", "station-heard.json")


def test_the_station_board_has_a_heard_panel():
    # its OWN board: next-passes looks now..now+24h, receptions live in the past
    d = json.load(open(BOARD, encoding="utf-8"))
    heard = [p for p in d["panels"] if (p.get("title") or "").startswith("Heard")]
    assert len(heard) == 1
    sql = heard[0]["targets"][0]["rawSql"]
    assert "FROM reception" in sql and "'$station'" in sql
    assert "$__timeFilter" in sql, "must follow the selected range, not a fixed window"
    assert heard[0]["type"] == "state-timeline"
    for t in heard[0]["targets"]:
        assert t["datasource"]["uid"] == "orbitcache", "pin the datasource (#320)"
