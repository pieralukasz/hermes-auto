"""iMessage/SMS source and its sorter path on a synthetic chat.db (no real Messages data, no model calls)."""
import json
import sqlite3

import pytest

from hermes_auto import cli, triage
from hermes_auto.config import defaults
from hermes_auto.hermes import deny_mode, session_title
from hermes_auto.runner import prompt_for, third_party
from hermes_auto.store import Store
from hermes_auto.triage import session_card
from hermes_auto_imessage import APPLE_EPOCH, Messages, Source

from test_triage import Fake

NOW = 1_800_000_000.0
QUIET = Source.defaults()["quiet_seconds"]

SCHEMA = """
CREATE TABLE handle (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL, service TEXT NOT NULL);
CREATE TABLE chat (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT UNIQUE NOT NULL, style INTEGER,
                   chat_identifier TEXT, service_name TEXT, display_name TEXT);
CREATE TABLE message (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT UNIQUE NOT NULL, text TEXT,
                      attributedBody BLOB, handle_id INTEGER DEFAULT 0, service TEXT, date INTEGER,
                      date_read INTEGER DEFAULT 0, is_from_me INTEGER DEFAULT 0, is_read INTEGER DEFAULT 0,
                      associated_message_type INTEGER DEFAULT 0, item_type INTEGER DEFAULT 0,
                      cache_has_attachments INTEGER DEFAULT 0);
CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER, message_date INTEGER DEFAULT 0,
                                PRIMARY KEY (chat_id, message_id));
CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER, UNIQUE(chat_id, handle_id));
"""


def apple(ts):
    return int((ts - APPLE_EPOCH) * 1_000_000_000)


class ChatDb:
    """A tiny chat.db with Messages' schema (the columns the adapter reads)."""

    def __init__(self, path):
        self.path = path
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)
        self.count = 0

    def handle(self, ident, service="iMessage"):
        return self.db.execute("INSERT INTO handle(id, service) VALUES (?,?)", (ident, service)).lastrowid

    def chat(self, guid, handles, name=None, group=False, service="iMessage"):
        chat_id = self.db.execute(
            "INSERT INTO chat(guid, style, chat_identifier, service_name, display_name) VALUES (?,?,?,?,?)",
            (guid, 43 if group else 45, guid.split(";")[-1], service, name or "")).lastrowid
        for handle in handles:
            self.db.execute("INSERT INTO chat_handle_join VALUES (?,?)", (chat_id, handle))
        self.db.commit()
        return chat_id

    def say(self, chat_id, text, at, sender=0, me=False, **columns):
        self.count += 1
        values = {"guid": f"m{self.count}", "text": text, "handle_id": sender, "service": "iMessage",
                  "date": apple(at), "is_from_me": int(me), **columns}
        rowid = self.db.execute(f"INSERT INTO message({','.join(values)}) VALUES ({','.join('?' * len(values))})",
                                list(values.values())).lastrowid
        self.db.execute("INSERT INTO chat_message_join VALUES (?,?,?)", (chat_id, rowid, values["date"]))
        self.db.commit()
        return rowid


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path, initialize=True)
    yield result
    result.close()


@pytest.fixture
def chats(tmp_path):
    db = ChatDb(tmp_path / "chat.db")
    yield db
    db.db.close()


def messages(chats, now, **settings):
    return Messages({**Source.defaults(), "db": str(chats.path), "contacts": False, **settings},
                    now=lambda: now)


def baseline(store, chats, now=NOW):
    candidates, key, cursor = messages(chats, now).inbox({}, store)
    assert candidates == []
    store.set_meta(key, cursor)


def test_baseline_skips_old_messages_then_a_new_one_from_someone_else_is_a_candidate(store, chats):
    ania = chats.handle("+48500100200")
    chat = chats.chat("iMessage;-;+48500100200", [ania])
    chats.say(chat, "stara wiadomość", NOW - 3600, sender=ania)
    baseline(store, chats)
    chats.say(chat, "Czy możesz jutro o 10?", NOW + 10, sender=ania)
    candidates, key, cursor = messages(chats, NOW + 10 + QUIET).inbox({}, store)
    assert len(candidates) == 1
    item = candidates[0]
    assert item["kind"] == "imessage" and item["thread_id"] == "iMessage;-;+48500100200"
    assert item["from"] == "+48500100200" and item["why"] == "dm" and item["chat_id"] == chat
    assert "NOWE" in item["body"] and "Czy możesz jutro o 10?" in item["body"]
    assert "stara wiadomość" in item["body"]  # earlier messages are context, not new
    assert not item["read"]
    store.set_meta(key, cursor)
    assert messages(chats, NOW + 300).inbox({}, store)[0] == []  # handed once


def test_own_messages_are_no_items_and_an_answer_after_the_burst_marks_it_read(store, chats):
    ania = chats.handle("+48500100200")
    chat = chats.chat("iMessage;-;+48500100200", [ania])
    baseline(store, chats)
    chats.say(chat, "piszę sam do siebie", NOW + 1, me=True)
    assert messages(chats, NOW + 1 + QUIET).inbox({}, store)[0] == []
    chats.say(chat, "Dzięki, a jutro?", NOW + 5, sender=ania)
    chats.say(chat, "Tak, o 10", NOW + 20, me=True)
    candidates, _, _ = messages(chats, NOW + 20 + QUIET).inbox({}, store)
    assert len(candidates) == 1 and candidates[0]["read"]  # answered in Messages: the sorter opens nothing


def test_a_burst_waits_for_quiet_and_holds_the_cursor(store, chats):
    ania, bartek = chats.handle("+48500100200"), chats.handle("bartek@example.test")
    a = chats.chat("iMessage;-;+48500100200", [ania])
    b = chats.chat("iMessage;-;bartek@example.test", [bartek])
    baseline(store, chats)
    chats.say(a, "pierwsza", NOW + 10, sender=ania)
    chats.say(b, "od Bartka", NOW + 11, sender=bartek)
    candidates, key, cursor = messages(chats, NOW + 11 + QUIET - 30).inbox({}, store)
    assert candidates == [] and store.get_meta("source_backlog:imessage")
    store.set_meta(key, cursor)
    chats.say(a, "druga", NOW + 40, sender=ania)
    candidates, key, cursor = messages(chats, NOW + 11 + QUIET).inbox({}, store)
    assert [c["from"] for c in candidates] == ["bartek@example.test"]  # Ania is still writing
    store.set_meta(key, cursor)
    candidates, key, cursor = messages(chats, NOW + 40 + QUIET).inbox({}, store)
    assert [c["from"] for c in candidates] == ["+48500100200"]
    assert candidates[0]["body"].count("NOWE") == 2
    store.set_meta(key, cursor)
    assert messages(chats, NOW + 400).inbox({}, store)[0] == [] and not store.get_meta("source_backlog:imessage")


def test_the_database_is_opened_read_only_and_a_denied_read_names_full_disk_access(tmp_path, chats):
    from hermes_auto_imessage import MessagesError, connect
    db = connect(chats.path)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        db.execute("INSERT INTO handle(id, service) VALUES ('x', 'SMS')")
    db.close()
    with pytest.raises(MessagesError, match="Full Disk Access"):
        connect(tmp_path / "missing" / "chat.db")


def test_groups_name_the_group_and_each_sender(store, chats):
    ania, bartek = chats.handle("+48500100200"), chats.handle("+48600100300")
    group = chats.chat("iMessage;+;chat123", [ania, bartek], name="Rodzina", group=True)
    unnamed = chats.chat("iMessage;+;chat456", [ania, bartek], group=True)
    baseline(store, chats)
    chats.say(group, "Obiad w niedzielę?", NOW + 10, sender=ania)
    chats.say(group, "Ja jestem", NOW + 12, sender=bartek)
    chats.say(unnamed, "hej", NOW + 13, sender=bartek)
    candidates, _, _ = messages(chats, NOW + 13 + QUIET).inbox({}, store)
    named, other = candidates
    assert named["why"] == "group" and named["group"] and not named["dm"]
    assert named["place"] == "grupa „Rodzina”" and named["from"] == "+48500100200, +48600100300"
    assert "+48500100200: Obiad" in named["body"] and "+48600100300: Ja jestem" in named["body"]
    assert other["place"] == "grupa: +48500100200, +48600100300" and other["thread_id"] == "iMessage;+;chat456"


def test_reactions_group_events_empty_and_late_synced_old_messages_are_left_out(store, chats):
    ania = chats.handle("+48500100200")
    chat = chats.chat("iMessage;-;+48500100200", [ania])
    baseline(store, chats)
    chats.say(chat, "Lubię „ok”", NOW + 10, sender=ania, associated_message_type=2000)
    chats.say(chat, None, NOW + 11, sender=ania, item_type=1)
    chats.say(chat, "", NOW + 12, sender=ania)
    chats.say(chat, "sprzed tygodnia, z synchronizacji iCloud", NOW - 7 * 86400, sender=ania)
    assert messages(chats, NOW + 400).inbox({}, store)[0] == []
    chats.say(chat, None, NOW + 500, sender=ania, cache_has_attachments=1)
    candidates, _, _ = messages(chats, NOW + 500 + QUIET).inbox({}, store)
    assert len(candidates) == 1 and "[załącznik]" in candidates[0]["body"]


def typedstream(text):
    """attributedBody as Messages writes it (text NULL on newer macOS): an NSString inside a typedstream."""
    raw = text.encode()
    size = bytes([len(raw)]) if len(raw) < 0x80 else b"\x81" + len(raw).to_bytes(2, "little")
    return (b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString\x00\x84\x84\x08NSObject"
            b"\x00\x85\x92\x84\x84\x84\x08NSString\x01\x94\x84\x01+" + size + raw
            + b"\x86\x84\x02iI\x01\x05\x92\x84\x84\x84\x0cNSDictionary\x00")


def test_text_only_in_attributed_body_is_decoded(store, chats):
    ania = chats.handle("+48500100200")
    chat = chats.chat("iMessage;-;+48500100200", [ania])
    baseline(store, chats)
    long = "Długa wiadomość " * 20
    chats.say(chat, None, NOW + 10, sender=ania, attributedBody=typedstream("Zadzwonisz? 📞"))
    chats.say(chat, None, NOW + 11, sender=ania, attributedBody=typedstream(long))
    chats.say(chat, None, NOW + 12, sender=ania, attributedBody=b"garbage")
    candidates, _, _ = messages(chats, NOW + 12 + QUIET).inbox({}, store)
    body = candidates[0]["body"]
    assert "Zadzwonisz? 📞" in body and long.strip() in body and body.count("NOWE") == 2


def address_book(tmp_path, people):
    folder = tmp_path / "AddressBook" / "Sources" / "ABC"
    folder.mkdir(parents=True)
    db = sqlite3.connect(folder / "AddressBook-v22.abcddb")
    db.executescript("CREATE TABLE ZABCDRECORD (Z_PK INTEGER PRIMARY KEY, ZFIRSTNAME TEXT, ZLASTNAME TEXT, "
                     "ZORGANIZATION TEXT, ZNICKNAME TEXT);"
                     "CREATE TABLE ZABCDPHONENUMBER (Z_PK INTEGER PRIMARY KEY, ZOWNER INTEGER, ZFULLNUMBER TEXT);"
                     "CREATE TABLE ZABCDEMAILADDRESS (Z_PK INTEGER PRIMARY KEY, ZOWNER INTEGER, ZADDRESS TEXT);")
    for pk, (first, last, org, phone, email) in enumerate(people, 1):
        db.execute("INSERT INTO ZABCDRECORD VALUES (?,?,?,?,NULL)", (pk, first, last, org))
        if phone:
            db.execute("INSERT INTO ZABCDPHONENUMBER(ZOWNER, ZFULLNUMBER) VALUES (?,?)", (pk, phone))
        if email:
            db.execute("INSERT INTO ZABCDEMAILADDRESS(ZOWNER, ZADDRESS) VALUES (?,?)", (pk, email))
    db.commit()
    db.close()
    return tmp_path / "AddressBook"


def test_senders_get_their_contact_names_when_contacts_are_readable(tmp_path, store, chats):
    book = address_book(tmp_path, [("Anna", "Nowak", None, "500 100 200", None),
                                   (None, None, "InPost", None, "info@inpost.test")])
    ania, inpost, other = chats.handle("+48500100200"), chats.handle("INFO@inpost.test"), chats.handle("+4870000")
    group = chats.chat("iMessage;+;chat1", [ania, inpost, other], group=True)
    baseline(store, chats)
    chats.say(group, "Hej", NOW + 10, sender=ania)
    chats.say(group, "Paczka", NOW + 11, sender=inpost)
    chats.say(group, "?", NOW + 12, sender=other)
    source = messages(chats, NOW + 12 + QUIET, contacts=True, contacts_dir=str(book))
    item = source.inbox({}, store)[0][0]
    assert item["from"] == "Anna Nowak, InPost, +4870000"
    assert item["place"] == "grupa: Anna Nowak, InPost, +4870000" and "Anna Nowak: Hej" in item["body"]
    # Contacts unreadable (no permission): handles instead, the source still works.
    store.set_meta("triage_cursor:imessage:local", None)
    baseline(store, chats, NOW + 12)
    chats.say(group, "dalej", NOW + 20, sender=ania)
    item = messages(chats, NOW + 20 + QUIET, contacts=True, contacts_dir=str(tmp_path / "none")).inbox({}, store)[0][0]
    assert item["from"] == "+48500100200"


def test_limit_and_a_long_scan_hold_the_cursor_and_flag_a_backlog(store, chats):
    handles = [chats.handle(f"+4850000000{n}") for n in range(4)]
    rooms = [chats.chat(f"iMessage;-;+4850000000{n}", [handle]) for n, handle in enumerate(handles)]
    baseline(store, chats)
    for n, room in enumerate(rooms):
        chats.say(room, f"wiadomość {n}", NOW + n, sender=handles[n])
    later = NOW + 3 + QUIET
    candidates, key, cursor = messages(chats, later).inbox({}, store, limit=2)
    assert [c["chat_id"] for c in candidates] == rooms[:2] and store.get_meta("source_backlog:imessage")
    store.set_meta(key, cursor)
    candidates, key, cursor = messages(chats, later).inbox({}, store, limit=2)
    assert [c["chat_id"] for c in candidates] == rooms[2:]
    store.set_meta(key, cursor)
    assert messages(chats, later).inbox({}, store)[0] == []
    for n, room in enumerate(rooms):
        chats.say(room, f"druga {n}", NOW + 10 + n, sender=handles[n])
    candidates, key, cursor = messages(chats, later + 20, max_scan=3).inbox({}, store)
    assert len(candidates) == 3 and store.get_meta("source_backlog:imessage")
    store.set_meta(key, cursor)
    assert [c["chat_id"] for c in messages(chats, later + 20, max_scan=3).inbox({}, store)[0]] == [rooms[3]]


def test_skipped_chats_never_reach_the_sorter(store, chats):
    ania = chats.handle("+48500100200")
    chat = chats.chat("iMessage;-;+48500100200", [ania])
    baseline(store, chats)
    chats.say(chat, "reklama", NOW + 10, sender=ania)
    source = messages(chats, NOW + 10 + QUIET, skip_chats=["iMessage;-;+48500100200"])
    assert source.inbox({}, store)[0] == []



def test_items_name_the_channel_and_the_chat_for_the_history_read(store, chats):
    ania = chats.handle("+48500100200", service="SMS")
    chat = chats.chat("SMS;-;+48500100200", [ania], service="SMS")
    baseline(store, chats)
    chats.say(chat, "Kod: 1234", NOW + 10, sender=ania, service="SMS")
    item = messages(chats, NOW + 10 + QUIET).inbox({}, store)[0][0]
    assert item["channel"] == "SMS" and item["chat_id"] == chat and item["place"] == "DM"


def test_read_in_messages_counts_only_with_skip_read(store, chats):
    ania = chats.handle("+48500100200")
    chat = chats.chat("iMessage;-;+48500100200", [ania])
    baseline(store, chats)
    chats.say(chat, "Przeczytane na telefonie", NOW + 10, sender=ania, is_read=1)
    assert not messages(chats, NOW + 10 + QUIET).inbox({}, store)[0][0]["read"]
    assert messages(chats, NOW + 10 + QUIET, skip_read=True).inbox({}, store)[0][0]["read"]


# ── Sorter path ──────────────────────────────────────────────────────────────


@pytest.fixture
def config():
    return {**defaults(), "language": "Polish", "sources": {"imessage": {"enabled": True}},
            "triage": {"enabled": True, "brief_times": ["07:45", "16:30"]}}


def dm_item(store, chats, text="Czy możesz jutro o 10?", at=NOW + 10):
    ania = chats.handle("+48500100200")
    chat = chats.chat("iMessage;-;+48500100200", [ania])
    baseline(store, chats)
    chats.say(chat, text, at, sender=ania)
    candidates, _, _ = messages(chats, at + QUIET).inbox({}, store)
    return chat, ania, candidates[0]


def test_sorted_like_other_message_sources_and_never_written_to():
    assert "imessage" in cli.MAIL_LIKE and triage.deny_profile({"kind": "imessage"}) is True
    assert triage.kind_of({"kind": "imessage"}) == "imessage"
    assert not hasattr(Source, "mark_read") and not hasattr(Source, "finish")  # never writes to Messages


def test_session_is_a_first_turn_matter_that_may_read_its_own_chat(store, chats, config):
    chat, _, item = dm_item(store, chats)
    fake = Fake(replies=[[{"i": 0, "decision": "session", "reason": "pyta o termin", "summary": "Ania pyta"}]])
    triage.triage_mail(store, config, fake, "imessage", "local", [item], "k", {"rowid": 1})
    system, user = fake.prompts[0]
    assert "komunikator" in system and user[0]["where"] == "iMessage · DM"
    assert "Czy możesz jutro o 10?" in user[0]["text"]
    job = store.rows()[0]
    assert job["source"] == "imessage" and deny_mode(job) is True and third_party(job)
    assert session_title(job) == "iMessage · +48500100200"
    assert store.db.execute("SELECT kind FROM triage_items").fetchone()[0] == "imessage"
    prompt = prompt_for(job, config)
    assert f"imsg history --chat-id {chat} --limit 30 --json" in prompt and "CHAT-" in prompt
    assert "Czy możesz jutro o 10?" in prompt and "Niczego nie zmieniaj ani nie wysyłaj" in prompt
    assert "NIGDY sam nie odpowiadaj" in prompt
    card = session_card(json.loads(job["payload"]), "pyta o termin", config)
    assert card.splitlines()[0] == "iMessage · +48500100200 napisał do Ciebie"
    assert "Czy możesz" not in card  # the card shows who and why, the text goes to the model only


def test_a_later_message_in_the_same_chat_follows_into_the_session(store, chats, config):
    chat, ania, item = dm_item(store, chats)
    fake = Fake(replies=[[{"i": 0, "decision": "session", "reason": "", "summary": ""}]])
    triage.triage_mail(store, config, fake, "imessage", "local", [item], "k", {"rowid": 1})
    session = store.rows()[0]["session_id"]
    chats.say(chat, "Albo o 11?", NOW + 500, sender=ania)
    store.set_meta("triage_cursor:imessage:local", {"rowid": item["rowids"][-1], "start": 0, "convs": {}})
    later = messages(chats, NOW + 500 + QUIET).inbox({}, store)[0]
    report = triage.triage_mail(store, config, Fake(), "imessage", "local", later, "k", {"rowid": 2})
    assert [(decision, detail) for decision, _, detail in report] == [("followup", session)]
    store.update(store.rows()[0]["id"], status="ready")  # the first session is answered; the reply goes on
    triage.process_followups(store, config, Fake(sessions={session: {}}))
    titles = [job["title"] for job in store.rows()]
    assert len(titles) == 2 and titles[1].startswith("↩ ")


def test_brief_lists_imessage_and_marks_nothing_read_in_messages(store, chats, config):
    _, _, item = dm_item(store, chats)
    fake = Fake(replies=[[{"i": 0, "decision": "skip", "reason": "", "summary": "kod SMS"}]])
    triage.triage_mail(store, config, fake, "imessage", "local", [item], "k", {"rowid": 1})
    assert triage.flush_read_marks(store, config, {"imessage": Source}) == []
    assert [row[0] for row in store.db.execute("SELECT status FROM read_marks")] == ["unsupported"]
    rows = [{**dict(row), "decision": "brief"} for row in store.db.execute("SELECT * FROM triage_items")]
    text = triage.render_brief(rows, config)
    assert "## Komunikatory (1)" in text and "kod SMS" in text and "iMessage" in text and "+48500100200" in text
