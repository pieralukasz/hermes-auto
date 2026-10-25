"""`routes`: one organisation's Slack and GitHub run on its own account, everything else on the default."""
import json

import pytest

from hermes_auto import bridge, triage
from hermes_auto.config import model_route, read_config
from hermes_auto.hermes import Hermes, job_route

WORK = {"provider": "work-account"}
CONFIG = {"routes": [
    {"match": {"source": "slack"}, **WORK},
    {"match": {"source": "github", "owner": "acme-org"}, **WORK},
]}


def test_rules_match_source_and_payload_case_insensitively():
    assert model_route(CONFIG, "slack", {"account": "T1"}) == WORK
    assert model_route(CONFIG, "github", {"owner": "ACME-org"}) == WORK
    assert model_route(CONFIG, "github", {"owner": "someone-else"}) == {}
    assert model_route(CONFIG, "gmail", {"owner": "acme-org"}) == {}
    assert model_route({}, "slack", {}) == {}


def test_job_route_reads_the_payload_and_keeps_replies_in_the_thread_on_the_same_account():
    reply = {"source": "github", "payload": json.dumps({"kind": "github", "owner": "acme-org",
                                                         "followup_of": {"id": "S-old"}})}
    assert job_route(reply, CONFIG) == WORK
    assert job_route({"source": "github", "payload": "not json"}, CONFIG) == {}
    assert job_route({"source": "todoist", "payload": "{}"}, CONFIG) == {}


def test_read_config_rejects_malformed_routes(tmp_path):
    for routes in ({"source": "slack"}, [{"provider": "x"}], [{"match": {}, "provider": "x"}],
                   [{"match": {"source": "slack"}}]):
        (tmp_path / "config.json").write_text(json.dumps({"routes": routes}))
        with pytest.raises(ValueError):
            read_config(tmp_path)
    (tmp_path / "config.json").write_text(json.dumps(CONFIG))
    assert read_config(tmp_path)["routes"] == CONFIG["routes"]


class Sorter:
    def __init__(self):
        self.calls = []

    def classify(self, system, user, max_tokens=0, route=None):
        sent = json.loads(user)
        self.calls.append((route, [entry.get("repo") for entry in sent]))
        return [{"i": entry["i"], "decision": "session", "reason": entry.get("repo") or "", "summary": ""}
                for entry in sent]


def github(owner, number):
    return {"kind": "github", "owner": owner, "place": f"{owner}/repo", "from": "a", "body": f"#{number}"}


def test_mixed_github_batch_is_classified_per_account_and_keeps_original_indexes():
    sorter = Sorter()
    items = [github("me", 0), github("acme-org", 1), github("me", 2)]
    decisions = triage.classify_mail(sorter, CONFIG, items)
    assert sorted(sorter.calls, key=lambda call: bool(call[0])) == [
        (None, ["me/repo", "me/repo"]), (WORK, ["acme-org/repo"])]
    assert {index: verdict["reason"] for index, verdict in decisions.items()} == {
        0: "me/repo", 1: "acme-org/repo", 2: "me/repo"}


def test_unrouted_batch_calls_the_classifier_without_a_route():
    sorter = Sorter()
    triage.classify_mail(sorter, {}, [github("me", 0)])
    assert sorter.calls == [(None, ["me/repo"])]


def test_bridge_passes_only_set_route_fields():
    assert bridge.route_kwargs({"route": {"provider": "work-account", "model": ""}}) == WORK
    assert bridge.route_kwargs({}) == {}


def test_bridge_classifier_uses_the_route_over_the_sorter_model(monkeypatch):
    import sys
    import types
    seen = {}

    def call_llm(**kwargs):
        seen.update(kwargs)
        message = types.SimpleNamespace(content="[]")
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])

    module = types.ModuleType("agent.auxiliary_client")
    module.call_llm = call_llm
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", module)
    config = {"triage": {"provider": "default-account", "model": "sorter-model"}}
    bridge.classify(config, {"system": "s", "user": "u", "route": WORK})
    assert (seen["provider"], seen["model"]) == ("work-account", "sorter-model")
    bridge.classify(config, {"system": "s", "user": "u"})
    assert (seen["provider"], seen["model"]) == ("default-account", "sorter-model")


def test_background_start_writes_the_route_into_the_request(tmp_path, monkeypatch):
    hermes = Hermes.__new__(Hermes)
    hermes.config = {**CONFIG, "run_budget_seconds": 1, "agent_run_budget_seconds": 1}
    hermes.home = tmp_path
    hermes.python = "python3"
    monkeypatch.setattr("hermes_auto.hermes.subprocess.Popen",
                        lambda *a, **k: type("P", (), {"pid": 1})())
    job = {"id": 7, "source": "slack", "mode": "draft", "payload": json.dumps({"kind": "slack"})}
    handle = hermes.start(job, "prompt")
    request = json.loads((tmp_path / "runs" / handle["dir"].split("/")[-1] / "request.json").read_text())
    assert request["route"] == WORK
