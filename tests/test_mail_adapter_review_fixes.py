"""Review 23.10.2026: Gmail backlog/boundary loss and Proton malformed charset. Synthetic data only."""
import pytest

from hermes_auto.store import Store
from hermes_auto_gmail import Gmail

BASE = 1_800_000_000_000


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path, initialize=True)
    yield result
    result.close()


class FakeGmail(Gmail):
    """Gmail list order is newest first, 100 ids per page; stamps maps id -> internalDate."""

    def __init__(self, stamps, repeat_token=False):
        self.stamps = dict(stamps)
        self.repeat_token = repeat_token
        self.list_calls = 0

    def get(self, path, **params):
        if path == "profile":
            return {"emailAddress": "me@a.test"}
        if path == "messages":
            self.list_calls += 1
            if self.list_calls > 1000:
                raise AssertionError("pagination loop")
            ids = sorted(self.stamps, key=lambda mid: (self.stamps[mid], mid), reverse=True)
            start = 0 if self.repeat_token else int(params.get("pageToken") or 0)
            page = ids[start:start + 100]
            nxt = start + 100
            more = {"nextPageToken": str(nxt)} if nxt < len(ids) else {}
            return {"messages": [{"id": mid} for mid in page], **more}
        if path.startswith("messages/"):
            mid = path.split("/")[1]
            return {"id": mid, "threadId": "t" + mid, "internalDate": str(self.stamps[mid]),
                    "labelIds": ["INBOX"], "payload": {"headers": [{"name": "From", "value": f"{mid}@b.test"}]}}
        if path.startswith("threads/"):
            return {"messages": []}
        raise AssertionError(path)


def drain(gmail, store, limit, runs=100):
    """Run the sorter loop like cli.run_triage: hand over, then persist the returned cursor."""
    seen = []
    for _ in range(runs):
        candidates, key, cursor = gmail.inbox({}, store, limit=limit)
        store.set_meta(key, cursor)
        if not candidates:
            break
        seen += [item["id"] for item in candidates]
    return seen


def start(store, ms):
    store.set_meta("triage_cursor:gmail:me@a.test", ms)


def test_gmail_backlog_over_500_hands_over_oldest_first_without_loss(store):
    gmail = FakeGmail({f"m{i:03d}": BASE + 1000 * (i + 1) for i in range(700)})
    start(store, BASE)
    first, _, _ = gmail.inbox({}, store, limit=40)
    assert [item["id"] for item in first][:3] == ["m000", "m001", "m002"]
    seen = drain(gmail, store, limit=40)
    assert sorted(seen) == sorted(gmail.stamps)
    assert len(seen) == len(set(seen))


def test_gmail_equal_stamps_at_limit_boundary_are_not_skipped(store):
    gmail = FakeGmail({"m0": BASE + 1000, "m1": BASE + 1000, "m2": BASE + 1000})
    start(store, BASE)
    seen = drain(gmail, store, limit=2)
    assert sorted(seen) == ["m0", "m1", "m2"]
    assert len(seen) == 3


def test_gmail_late_message_with_earlier_stamp_inside_overlap_is_handed_over(store):
    gmail = FakeGmail({"a": BASE + 10_000})
    start(store, BASE)
    assert drain(gmail, store, limit=40) == ["a"]
    # Delivered after the run, stamped 30 s before the cursor (inside the query's 120 s overlap).
    gmail.stamps["late"] = BASE + 10_000 - 30_000
    assert drain(gmail, store, limit=40) == ["late"]


def test_gmail_repeated_page_token_fails_instead_of_looping(store):
    gmail = FakeGmail({f"m{i:03d}": BASE + 1000 * (i + 1) for i in range(250)}, repeat_token=True)
    start(store, BASE)
    with pytest.raises(RuntimeError, match="page"):
        gmail.inbox({}, store, limit=40)
    assert store.get_meta("triage_cursor:gmail:me@a.test") == BASE


def test_gmail_first_run_records_baseline_and_hands_over_nothing(store):
    gmail = FakeGmail({"old": 1_000_000_000_000})
    candidates, key, cursor = gmail.inbox({}, store, limit=40)
    store.set_meta(key, cursor)
    assert candidates == []
    gmail.stamps["new"] = 10 ** 13  # far future
    assert drain(gmail, store, limit=40) == ["new"]


# --- Proton -------------------------------------------------------------------------------------

from hermes_auto_proton import Proton, parse_candidate, parse_event  # noqa: E402


def raw_mail(mid, charset="x-nonexistent", body=b"Tre\xc5\x9b\xc4\x87 odpowiedzi", sender="jan@b.test"):
    return (f"Message-ID: <{mid}@b.test>\r\nFrom: {sender}\r\nIn-Reply-To: <root@a.test>\r\n"
            f"References: <root@a.test>\r\nSubject: Re: {mid}\r\n"
            f"Content-Type: text/plain; charset=\"{charset}\"\r\n\r\n").encode() + body + b"\r\n"


def test_proton_parse_event_unknown_charset_keeps_message_and_body():
    event = parse_event(raw_mail("r1"), ["me@a.test"])
    assert event["id"] == "<r1@b.test>"
    assert event["eligible"]
    assert "odpowiedzi" in event["payload"]["body"]


def test_proton_parse_candidate_unknown_charset_keeps_body():
    item = parse_candidate(raw_mail("r1"), {"me@a.test"})
    assert "odpowiedzi" in item["body"]


class FakeProton(Proton):
    def __init__(self, mailboxes):
        self.mailboxes, self.current = mailboxes, None

    def select(self, mailbox):
        if mailbox not in self.mailboxes:
            raise RuntimeError("missing")
        self.current = mailbox

    def search(self, *criteria):
        return [str(uid).encode() for uid in self.mailboxes[self.current]]

    def fetch(self, uid, headers=False):
        return self.mailboxes[self.current][int(uid)]


def test_proton_poll_one_malformed_message_does_not_block_others(store):
    root = (b"Message-ID: <root@a.test>\r\nFrom: me@a.test\r\nSubject: x\r\n"
            b"Content-Type: text/plain\r\n\r\nhi\r\n")
    settings = {"username": "me@a.test", "watch_mailbox": "W", "all_mailbox": "All"}
    proton = FakeProton({"W": {1: root}, "All": {1: root}})
    proton.poll(settings, store)  # baseline
    proton.mailboxes["All"].update({2: raw_mail("bad"), 3: raw_mail("good", charset="utf-8")})
    proton.poll(settings, store)
    assert sorted(row["external_id"] for row in store.rows()) == ["<bad@b.test>", "<good@b.test>"]
