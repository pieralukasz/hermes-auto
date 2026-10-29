"""The first, automatic turn's prompt names only the reads the guard grants the session's account (config.json
`first_turn`; synthetic config: a work org on its own account, which also reads Todoist) and asks for no draft."""
from hermes_auto import triage

WORK = "work-account"
CONFIG = {
    "language": "Polish",
    "routes": [
        {"match": {"source": "slack", "dm": True}, "provider": "private-account"},
        {"match": {"source": "slack"}, "provider": WORK},
        {"match": {"source": "github", "owner": "acme-work"}, "provider": WORK},
        {"match": {"source": "linear"}, "provider": WORK},
    ],
    "first_turn": {"reads": ["github", "google", "linear", "slack", "todoist"],
                   "providers": {WORK: {"reads": ["github", "linear", "slack", "todoist"], "session_search": False}}},
}


def github(owner, **extra):
    return {"kind": "github", "id": "1@x", "from": "dev", "place": f"{owner}/web#12", "date": "2026-10-26",
            "url": f"https://github.com/{owner}/web/pull/12", "body": "Please review", "why": "review_requested",
            "owner": owner, **extra}


def slack_channel(**extra):
    return {"kind": "slack", "id": "C1:1.0@1.0", "thread_id": "C1:1.0", "from": "Chris", "place": "#dev",
            "date": "2026-10-26", "why": "mention", "channel": "C1", "thread_ts": "1.0", "dm": False,
            "private": False, "redacted": False, "body": "NOWE Chris: zerkniesz?", **extra}


def linear(**extra):
    return {"kind": "linear", "id": "L1", "from": "qa", "place": "WEB-1", "date": "2026-10-26",
            "why": "issueNewComment", "body": "QA FAIL", **extra}


PRIVATE_ONLY = ("session_search", "calendar list", "kalendarz", "gmail", "Gmail")


def work_prompts(config=CONFIG):
    return (triage.mail_session_prompt(github("acme-work"), "", config),
            triage.mail_session_prompt(slack_channel(), "", config),
            triage.mail_session_prompt(linear(), "", config))


def test_work_account_prompts_name_only_work_reads():
    for prompt in work_prompts():
        for word in PRIVATE_ONLY:
            assert word not in prompt.split("<<<")[0], word
        assert "`gh` (pr view" in prompt and "`linear` (issue view" in prompt and "slack_read_thread" in prompt
        assert "td task list --filter 'search: <fraza>' --json" in prompt  # Todoist: the user agreed


def test_private_account_keeps_the_calendar_gmail_and_history():
    prompt = triage.mail_session_prompt(github("someone-else"), "", CONFIG)
    assert "td task list --filter 'search: <fraza>' --json" in prompt and "session_search" in prompt
    assert "calendar list" in prompt and "gmail search" in prompt


def test_prompt_does_not_ask_to_report_empty_lookups():
    prompt = triage.mail_session_prompt(github("someone-else"), "", CONFIG)
    assert "Jeśli nie znajdziesz, nie pisz o tym" in prompt


def test_first_turn_prompts_present_without_drafting_or_handing_off():
    for prompt in work_prompts():
        assert "hermes_auto_read" in prompt and "1–3 zdaniach" in prompt and "Bez szkicu" in prompt
        assert "przekaż do" not in prompt and "delegate_task" not in prompt and "bloku kodu" not in prompt
    assert "NIGDY sam nic nie publikuj na GitHubie" in triage.mail_session_prompt(github("acme-work"), "", CONFIG)


def test_followup_without_search_does_not_point_at_session_search():
    origin = {"id": "s1", "title": "GitHub · review", "date": "2026-10-25"}
    prompt = triage.mail_session_prompt(github("acme-work", followup_of=origin), "", CONFIG)
    assert "session_search" not in prompt.split("<<<")[0] and "Tamtej rozmowy nie ruszam" in prompt
    prompt = triage.mail_session_prompt(github("someone-else", followup_of=origin), "", CONFIG)
    assert "więcej kontekstu znajdziesz przez session_search" in prompt


def test_the_jobs_source_picks_the_route():
    """A mail item has no `kind`; the job's source decides its route, as for the session itself."""
    config = {**CONFIG, "routes": [{"match": {"source": "gmail"}, "provider": WORK}]}
    mail = {"from": "a@example.test", "subject": "S", "body": "b"}
    assert "gmail search" in triage.mail_session_prompt(mail, "", config)
    assert "gmail search" not in triage.mail_session_prompt(mail, "", config, source="gmail")


def test_without_a_first_turn_section_every_read_stays():
    prompt = triage.mail_session_prompt(github("acme-work"), "", {"language": "Polish", "routes": CONFIG["routes"]})
    assert "td task list" in prompt and "session_search" in prompt and "slack_read_thread" in prompt
    assert "calendar list" in prompt
