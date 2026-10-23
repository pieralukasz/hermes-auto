"""profile_routes: jobs of a matching source item run in another Hermes profile (synthetic data only)."""
import json

import pytest

from hermes_auto.hermes import Hermes
from hermes_auto.profiles import config_for, profile_for

RULE = {"source": "todoist", "match": {"projectId": ["p-work"]}, "profile": "work",
        "since": "2026-10-23 18:30:00"}


def job(project, created="2026-10-24 08:00:00", source="todoist", sid="s1"):
    return {"id": 1, "source": source, "created": created, "session_id": sid,
            "payload": json.dumps({"projectId": project})}


def config(tmp_path, routes=(RULE,)):
    home = tmp_path / "home"
    (home / "profiles" / "work").mkdir(parents=True)
    (home / "profiles" / "work" / "config.yaml").write_text("model: {}\n")
    source = tmp_path / "src"
    source.mkdir()
    (source / "hermes_state_registry.py").write_text("")
    return {"hermes_home": str(home), "hermes_runtime_home": str(home), "hermes_source": str(source),
            "hermes_python": "/bin/false", "profile_routes": list(routes)}


def test_matching_job_routes_to_profile_but_keeps_root_runtime(tmp_path):
    cfg = config(tmp_path)
    assert profile_for(job("p-work"), cfg) == "work"
    routed = config_for(cfg, "work")
    assert routed["hermes_home"].endswith("/profiles/work")
    assert routed["hermes_runtime_home"] == cfg["hermes_runtime_home"]


def test_other_project_source_and_older_jobs_stay_in_default(tmp_path):
    cfg = config(tmp_path)
    assert profile_for(job("p-private"), cfg) is None
    assert profile_for(job("p-work", source="gmail"), cfg) is None
    assert profile_for(job("p-work", created="2026-10-23 10:00:00"), cfg) is None
    assert profile_for(None, cfg) is None and profile_for({"session_id": "x"}, cfg) is None


def test_missing_profile_fails_closed(tmp_path):
    cfg = config(tmp_path, routes=({**RULE, "profile": "gone"},))
    with pytest.raises(RuntimeError, match="does not exist"):
        config_for(cfg, profile_for(job("p-work"), cfg))


class Recording(Hermes):
    def __init__(self, cfg, home, homes):
        super().__init__(cfg, home)
        self.homes, self.calls = homes, []

    def invoke(self, action, job=None, *, profile_config=None, **extra):
        cfg = profile_config or self.config_for_job(job)
        self.calls.append((action, cfg["hermes_home"]))
        if action == "archive_states":
            known = self.homes.get(cfg["hermes_home"], {})
            return {"states": {sid: known.get(sid) for sid in extra["session_ids"]}}
        return {"exists": True}


def test_session_calls_and_archive_states_follow_the_job_profile(tmp_path):
    cfg = config(tmp_path)
    work = config_for(cfg, "work")["hermes_home"]
    hermes = Recording(cfg, tmp_path, {cfg["hermes_home"]: {"a": False}, work: {"b": True}})
    assert hermes.exists(job("p-work"))
    assert hermes.calls[-1] == ("inspect", work)
    assert hermes.archive_states(["a", "b", "c"]) == {"a": False, "b": True, "c": None}
    assert [home for action, home in hermes.calls if action == "archive_states"] == [cfg["hermes_home"], work]
