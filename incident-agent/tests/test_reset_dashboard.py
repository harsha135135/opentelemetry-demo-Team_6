"""reset-dashboard archives (never deletes) old runs and leaves an empty incidents table."""
import json

import cli


def test_reset_archives_runs_and_rebuilds_empty(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    (runs / "INC-OLD-1").mkdir(parents=True)
    (runs / "INC-OLD-1" / "INC-OLD-1_report.json").write_text("{}")
    built = {}
    monkeypatch.setattr(cli, "build_grafana_dashboards", lambda r: built.setdefault("runs", r) and "built")
    monkeypatch.setattr(cli, "reload_grafana_dashboards", lambda: "reloaded")
    assert cli.main(["reset-dashboard", "--out", str(runs)]) == 0
    archived = list((runs / "_archive").glob("*/INC-OLD-1/INC-OLD-1_report.json"))
    assert len(archived) == 1 and not (runs / "INC-OLD-1").exists()   # moved, not deleted
    assert built["runs"] == runs.resolve()
    assert json.loads(archived[0].read_text()) == {}
