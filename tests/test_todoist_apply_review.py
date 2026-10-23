import copy

import pytest

from hermes_auto_todoist import apply_decision


def test_apply_preserves_fresh_labels_and_moves_last():
    current = {"id": "A", "labels": ["new-label"], "projectId": "inbox", "priority": 1}
    commands = []

    def read(command):
        return copy.deepcopy(current)

    def write(command):
        commands.append(command)
        if "--labels" in command:
            current["labels"] = command[command.index("--labels") + 1].split(",")
        if "--priority" in command:
            current["priority"] = 3
        if "--project" in command:
            current["projectId"] = "P"

    apply_decision("td", {"id": "A", "labels": ["old-label"]},
                   {"project_id": "P", "priority": "p2", "agent": True}, "hermes", read, write)
    assert set(current["labels"]) == {"new-label", "hermes"}
    assert commands[-1][2] == "move"


@pytest.mark.parametrize("field", ["labels", "priority", "sectionId"])
def test_apply_verifies_all_requested_fields(field):
    current = {"id": "A", "labels": [], "projectId": "P", "priority": 1, "sectionId": None}

    def read(command):
        return copy.deepcopy(current)

    def write(command):
        if "--labels" in command and field != "labels":
            current["labels"] = ["hermes"]
        if "--priority" in command and field != "priority":
            current["priority"] = 3
        if "--section" in command and field != "sectionId":
            current["sectionId"] = "S"

    with pytest.raises(RuntimeError):
        apply_decision("td", {"id": "A", "labels": []},
                       {"project_id": "P", "section_id": "S", "priority": "p2", "agent": True},
                       "hermes", read, write)


def test_retry_with_existing_agent_label_still_requests_run():
    current = {"id": "A", "labels": ["hermes"], "projectId": "P", "priority": 1}
    applied = apply_decision("td", current, {"project_id": "P", "agent": True}, "hermes",
                             lambda _: dict(current), lambda _: None)
    assert applied["run"] is True


def test_completed_task_is_not_modified():
    commands = []
    with pytest.raises(RuntimeError):
        apply_decision("td", {"id": "A"}, {"priority": "p2"}, "hermes",
                       lambda _: {"id": "A", "checked": True}, commands.append)
    assert commands == []
