"""Malformed headers must not stall push ingestion or mark another message."""
from hermes_auto.store import Store
from hermes_auto_proton import Proton


def raw(mid, refs=""):
    return (f"From: sender@example.test\r\nMessage-ID: <{mid}@example.test>\r\n"
            f"References: {refs}\r\nSubject: Question\r\n\r\nHello").encode()


class Inbox(Proton):
    def __init__(self, messages):
        self.messages = messages
        self.current = "INBOX"
        self.searches = []

    def inbox_status(self):
        return 7, max(self.messages, default=0) + 1

    def select(self, mailbox):
        self.current = mailbox

    def search(self, *criteria):
        self.searches.append(criteria)
        for value in criteria:
            value.encode("ascii")
        if self.current == "Sent" or "SEEN" in criteria:
            return []
        return [str(uid).encode() for uid in self.messages]

    def fetch(self, uid, headers=False):
        return self.messages[int(uid)]


def test_non_ascii_reference_does_not_block_later_mail(tmp_path):
    store = Store(tmp_path, initialize=True)
    key = "triage_cursor:proton:me@example.test"
    store.set_meta(key, {"validity": 7, "uid": 9})
    client = Inbox({10: raw("one", "<zażółć@example.test>"), 11: raw("two")})
    items, _, cursor = client.inbox({"username": "me@example.test"}, store)
    assert len(items) == 2
    assert cursor["uid"] == 11
    assert not any("HEADER" in query for query in client.searches)
    store.close()


class MarkClient:
    def __init__(self, validity=7):
        self.validity = validity
        self.marked = []

    def select(self, *args, **kwargs):
        return "OK", []

    def status(self, *args):
        return "OK", [f"INBOX (UIDVALIDITY {self.validity} UIDNEXT 20)".encode()]

    def uid(self, operation, *args):
        if operation == "search":
            return "OK", [b"10"]
        assert operation == "store"
        self.marked.append(args[0])
        return "OK", []


def test_unicode_message_is_marked_by_scoped_uid(tmp_path):
    store = Store(tmp_path, initialize=True)
    store.set_meta("triage_cursor:proton:me@example.test", {"validity": 7, "uid": 9})
    client = Inbox({10: raw("zażółć")})
    items, _, _ = client.inbox({"username": "me@example.test"}, store)
    assert items[0]["imap_uid"] == 10
    assert items[0]["imap_uidvalidity"] == 7
    marker = Proton.__new__(Proton)
    marker.client = MarkClient()
    assert marker.mark_seen(items) == {items[0]["id"]: "done"}
    assert marker.client.marked == [b"10"]
    store.close()


def test_missing_uid_is_not_reported_as_marked():
    marker = Proton.__new__(Proton)
    marker.client = MarkClient()
    item = {"id": "<gone@example.test>", "imap_uid": 11, "imap_uidvalidity": 7}
    assert marker.mark_seen([item]) == {item["id"]: "unsupported"}
    assert marker.client.marked == []


def test_proton_backlog_requests_another_push_tick(tmp_path):
    store = Store(tmp_path, initialize=True)
    key = "triage_cursor:proton:me@example.test"
    store.set_meta(key, {"validity": 7, "uid": 9})
    client = Inbox({10: raw("one"), 11: raw("two")})
    items, _, cursor = client.inbox({"username": "me@example.test"}, store, limit=1)
    assert len(items) == 1
    assert store.get_meta("source_backlog:proton") is True
    store.set_meta(key, cursor)
    items, _, _ = client.inbox({"username": "me@example.test"}, store, limit=1)
    assert len(items) == 1
    assert store.get_meta("source_backlog:proton") is False
    store.close()


def test_non_ascii_watched_root_does_not_block_poll(tmp_path):
    from test_mail_adapter_review_fixes import FakeProton

    class StrictSearch(FakeProton):
        def search(self, *criteria):
            for value in criteria:
                value.encode("ascii")
            return super().search(*criteria)

    client = StrictSearch({"Watch": {1: raw("zażółć")}, "All": {1: raw("valid")}})
    store = Store(tmp_path, initialize=True)
    client.poll({"username": "me@example.test", "watch_mailbox": "Watch", "all_mailbox": "All"}, store)
    assert store.rows() == []
    store.close()


def test_uidvalidity_change_and_legacy_marks_do_not_write():
    marker = Proton.__new__(Proton)
    marker.client = MarkClient(validity=8)
    items = [{"id": "<old@example.test>", "imap_uid": 10, "imap_uidvalidity": 7},
             {"id": "<legacy@example.test>"}]
    assert marker.mark_seen(items) == {item["id"]: "unsupported" for item in items}
    assert marker.client.marked == []
