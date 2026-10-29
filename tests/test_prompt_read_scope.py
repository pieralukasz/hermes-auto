"""Session prompts of read-only research sessions name only the reads their bot/account scope allows and leave
the draft to the bot the matter is handed to (synthetic config: a work org on its own account)."""
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
    "bots": {
        "default": "default", "inbox": "inbox",
        "read_only": {"inbox": {"reads": ["github", "google", "linear", "slack", "todoist"],
                                "providers": {WORK: {"reads": ["github", "linear", "slack"],
                                                     "session_search": False}}}},
    },
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


PRIVATE_ONLY = ("td task list", "Todoist", "session_search", "calendar list", "kalendarz")


def test_work_account_prompts_name_only_work_reads():
    for prompt in (triage.mail_session_prompt(github("acme-work"), "", CONFIG),
                   triage.mail_session_prompt(slack_channel(), "", CONFIG),
                   triage.mail_session_prompt(linear(), "", CONFIG)):
        for word in PRIVATE_ONLY:
            assert word not in prompt.split("<<<")[0], word
        assert "`gh` (pr view" in prompt and "`linear` (issue view" in prompt and "slack_read_thread" in prompt


def test_private_account_keeps_todoist_lookup_and_history():
    prompt = triage.mail_session_prompt(github("someone-else"), "", CONFIG)
    assert "td task list --filter 'search: <fraza>' --json" in prompt and "session_search" in prompt
    assert "kalendarz (`google_api.py calendar list`)" in prompt


def test_research_prompts_hand_off_instead_of_drafting():
    for prompt in (triage.mail_session_prompt(github("acme-work"), "", CONFIG),
                   triage.mail_session_prompt(slack_channel(), "", CONFIG),
                   triage.mail_session_prompt(linear(), "", CONFIG)):
        assert "bloku kodu" not in prompt and "przekaż do <bot>" in prompt and "Gotowego szkicu nie pisz" in prompt
    assert "NIGDY sam nic nie publikuj na GitHubie" in triage.mail_session_prompt(github("acme-work"), "", CONFIG)


def test_followup_without_search_does_not_point_at_session_search():
    origin = {"id": "s1", "title": "GitHub · review", "date": "2026-10-25"}
    prompt = triage.mail_session_prompt(github("acme-work", followup_of=origin), "", CONFIG)
    assert "session_search" not in prompt.split("<<<")[0] and "po przekazaniu sprawy" in prompt
    prompt = triage.mail_session_prompt(github("someone-else", followup_of=origin), "", CONFIG)
    assert "kontekst znajdziesz przez session_search" in prompt


def test_reply_in_a_conversation_uses_that_bots_scope():
    config = {**CONFIG, "bots": {**CONFIG["bots"], "read_only": {
        **CONFIG["bots"]["read_only"], "narrow": {"reads": ["github"]}}}}
    prompt = triage.mail_session_prompt(github("someone-else", bot="narrow"), "", config)
    assert "`gh` (pr view" in prompt and "td task list" not in prompt and "slack_read_thread" not in prompt


def test_without_bots_every_read_stays():
    prompt = triage.mail_session_prompt(github("acme-work"), "", {"language": "Polish", "routes": CONFIG["routes"]})
    assert "td task list" in prompt and "session_search" in prompt and "slack_read_thread" in prompt
