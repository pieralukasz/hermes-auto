"""Opt-in contract check with installed Hermes, no network and no model calls."""
import os

import pytest

from hermes_auto.config import defaults
from hermes_auto.hermes import Hermes
from hermes_auto.store import Store


@pytest.mark.skipif(not os.environ.get("HERMES_AUTO_TEST_PYTHON"), reason="local Hermes runtime not configured")
def test_real_session_api_two_profiles(tmp_path):
    ids = []
    for name in ("profile-a", "profile-b", "profile-a"):
        home = tmp_path / name
        home.mkdir(exist_ok=True)
        hermes_home = home / "hermes"
        hermes_home.mkdir(exist_ok=True)
        config = {**defaults(), "hermes_home": str(hermes_home),
                  "hermes_python": os.environ["HERMES_AUTO_TEST_PYTHON"],
                  "hermes_source": os.environ["HERMES_AUTO_TEST_SOURCE"],
                  "triage": {"hide_sessions": True}}
        store = Store(home, initialize=True)
        try:
            store.enqueue("example", "todoist", "x", "Integration test", "open", {})
            store.db.commit()
            job = store.rows()[0]
            hermes = Hermes(config, home)
            assert hermes.invoke("doctor")["ok"]
            hermes.create(job, "Synthetic test context")
            hermes.create(job, "Synthetic test context")
            info = hermes.invoke("inspect", job)
            assert (info["exists"], info["messages"], info["archived"]) == (True, 2, False)
            assert hermes.invoke("inspect", job, needles=["Synthetic test context"])["delivered"] is True
            assert hermes.invoke("inspect", job, needles=["not delivered"])["delivered"] is False
            assert hermes.archive_states([job["session_id"], "missing"]) == {
                job["session_id"]: False, "missing": None}
            assert hermes.unarchive(job["session_id"])["archived"] is False
            assert hermes.find_session(subject="Synthetic subject", sent=[{"ts": 1.0, "to": []}]) is None
            ids.append(job["session_id"])
        finally:
            store.close()
    assert ids[0] == ids[2] and ids[0] != ids[1]
