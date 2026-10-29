"""Matrix source (WhatsApp, Messenger, Instagram, LinkedIn through mautrix bridges on the user's homeserver) and its
sorter path, on a fake homeserver (no network, no real rooms, no model calls)."""
import json
import os
import urllib.parse

import pytest

from hermes_auto import cli, triage
from hermes_auto.config import defaults
from hermes_auto.hermes import deny_mode, session_title
from hermes_auto.runner import prompt_for, third_party
from hermes_auto.store import Store
from hermes_auto.triage import session_card
from hermes_auto_matrix import Matrix, MatrixError, Source, history_lines, read_token

from test_triage import Fake

NOW = 1_800_000_000.0
QUIET = Source.defaults()["quiet_seconds"]
ME = "@me:example.test"
DM = "!dmAnnaAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
GROUP = "!groupRodzinaBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
IG = "!dmInstaCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"
MGMT = "!mgmtWhatsappDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD"
SPACE = "!spaceEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEE"
ANNA, BARTEK, IGOR = "@whatsapp_48500100200:example.test", "@whatsapp_48600100300:example.test", "@instagram_17:example.test"


def bridge_state(protocol, name, room_type):
    content = {"bridgebot": f"@{protocol.removesuffix('go')}bot:example.test",
               "protocol": {"id": protocol, "displayname": name}, "channel": {"id": "x"}}
    if room_type:
        content["com.beeper.room_type"] = room_type
    return {"type": "m.bridge", "state_key": f"fi.mau.{protocol}://x", "content": content}


class Server:
    """A homeserver with a few bridged rooms: /sync by position, /messages backwards, room state, members."""

    def __init__(self):
        self.events = []  # (room, event) in arrival order
        self.rooms = {}
        self.calls = []
        self.room(DM, [bridge_state("whatsapp", "WhatsApp", "dm"), name("Anna Nowak")], {ANNA: "Anna Nowak"})
        self.room(GROUP, [bridge_state("whatsapp", "WhatsApp", None), name("Rodzina")],
                  {ANNA: "Anna Nowak", BARTEK: "Bartek"})
        self.room(IG, [bridge_state("instagramgo", "Instagram", "dm"), name("igor")], {IGOR: "igor"})
        self.room(MGMT, [], {"@whatsappbot:example.test": "WhatsApp bridge bot"})
        self.room(SPACE, [bridge_state("whatsapp", "WhatsApp", None),
                          {"type": "m.room.create", "state_key": "", "content": {"type": "m.space"}}], {})

    def room(self, room_id, state, members):
        members = {ME: "Łukasz", **members}
        self.rooms[room_id] = {"state": state + [
            {"type": "m.room.member", "state_key": user, "content": {"membership": "join", "displayname": shown}}
            for user, shown in members.items()], "members": members}

    def say(self, room_id, sender, text, at, **content):
        event = {"type": "m.room.message", "sender": sender, "event_id": f"$e{len(self.events)}",
                 "origin_server_ts": int(at * 1000), "content": {"msgtype": "m.text", "body": text, **content}}
        self.events.append((room_id, event))
        return event

    def get(self, path, params=None):
        params = params or {}
        self.calls.append((path, params))
        path = urllib.parse.unquote(path)
        if path == "/_matrix/client/v3/account/whoami":
            return {"user_id": ME, "device_id": "hermes-auto"}
        if path == "/_matrix/client/v3/sync":
            start = int(params.get("since") or len(self.events))
            timeline = {}
            limit = json.loads(params["filter"])["room"]["timeline"]["limit"]
            for room_id, event in self.events[start:]:
                timeline.setdefault(room_id, []).append(event)
            join = {room: {"timeline": {"events": events[-limit:] if limit else [],
                                        "limited": len(events) > limit}}
                    for room, events in timeline.items()}
            return {"next_batch": str(len(self.events)), "rooms": {"join": join}}
        room_id, _, rest = path.removeprefix("/_matrix/client/v3/rooms/").partition("/")
        if rest == "state":
            return self.rooms[room_id]["state"]
        if rest == "joined_members":
            return {"joined": {user: {"display_name": shown} for user, shown in self.rooms[room_id]["members"].items()}}
        if rest == "messages":
            events = [event for room, event in self.events if room == room_id]
            return {"chunk": list(reversed(events))[:int(params.get("limit", 10))]}
        raise AssertionError(f"unexpected GET {path}")


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path, initialize=True)
    yield result
    result.close()


@pytest.fixture
def server():
    return Server()


def name(text):
    return {"type": "m.room.name", "state_key": "", "content": {"name": text}}


def matrix(server, now, **settings):
    return Matrix({**Source.defaults(), **settings}, http=server, now=lambda: now)


def baseline(store, server, now=NOW):
    candidates, key, cursor = matrix(server, now).inbox({}, store)
    assert candidates == []
    store.set_meta(key, cursor)


def check(store, server, now, **settings):
    candidates, key, cursor = matrix(server, now, **settings).inbox({}, store)
    store.set_meta(key, cursor)
    return candidates


# ── Reading the homeserver ───────────────────────────────────────────────────

def test_baseline_reads_no_messages_then_a_new_whatsapp_dm_is_a_candidate(store, server):
    server.say(DM, ANNA, "stara wiadomość", NOW - 3600)
    baseline(store, server)
    sync = [params for path, params in server.calls if path.endswith("/sync")]
    assert json.loads(sync[0]["filter"])["room"]["timeline"]["limit"] == 0  # the baseline fetches no message
    server.say(DM, ANNA, "Czy możesz jutro o 10?", NOW + 10)
    candidates = check(store, server, NOW + 10 + QUIET)
    assert len(candidates) == 1
    item = candidates[0]
    assert item["kind"] == "matrix" and item["thread_id"] == DM and item["room_id"] == DM
    assert item["channel"] == "WhatsApp" and item["why"] == "dm" and item["place"] == "DM"
    assert item["from"] == "Anna Nowak" and item["account"] == ME and not item["read"]
    assert "NOWE" in item["body"] and "Anna Nowak: Czy możesz jutro o 10?" in item["body"]
    assert "stara wiadomość" in item["body"] and "NOWE" not in item["body"].splitlines()[0]
    assert check(store, server, NOW + 400) == []  # handed once


def test_own_messages_bridge_bots_management_rooms_and_spaces_are_no_items(store, server):
    baseline(store, server)
    server.say(DM, ME, "piszę z telefonu (double puppet)", NOW + 1)
    server.say(DM, "@whatsapp_48111111111:example.test", "moje konto WhatsApp (puppet)", NOW + 2)
    server.say(MGMT, "@whatsappbot:example.test", "Bridge status: connected", NOW + 3)
    server.say(SPACE, ANNA, "w przestrzeni", NOW + 4)
    server.say(DM, ANNA, "notice", NOW + 5, msgtype="m.notice")
    server.say(DM, ANNA, "* poprawka", NOW + 6, **{"m.relates_to": {"rel_type": "m.replace", "event_id": "$x"}})
    later = NOW + 6 + QUIET
    assert check(store, server, later, own_puppets=["@whatsapp_48111111111:example.test"]) == []


def test_an_answer_after_the_burst_marks_it_read(store, server):
    baseline(store, server)
    server.say(DM, ANNA, "Dzięki, a jutro?", NOW + 5)
    server.say(DM, ME, "Tak, o 10", NOW + 20)
    candidates = check(store, server, NOW + 20 + QUIET)
    assert len(candidates) == 1 and candidates[0]["read"]


def test_a_burst_waits_for_quiet_and_comes_as_one_item(store, server):
    baseline(store, server)
    server.say(DM, ANNA, "pierwsza", NOW + 10)
    server.say(IG, IGOR, "hej z Instagrama", NOW + 11)
    assert check(store, server, NOW + 11 + QUIET - 30) == [] and store.get_meta("source_backlog:matrix")
    server.say(DM, ANNA, "druga", NOW + 40)
    assert [c["channel"] for c in check(store, server, NOW + 11 + QUIET)] == ["Instagram"]
    candidates = check(store, server, NOW + 40 + QUIET)
    assert [c["room_id"] for c in candidates] == [DM] and candidates[0]["body"].count("NOWE") == 2
    assert check(store, server, NOW + 400) == [] and not store.get_meta("source_backlog:matrix")


def test_groups_name_the_group_and_each_sender(store, server):
    baseline(store, server)
    server.say(GROUP, ANNA, "Obiad w niedzielę?", NOW + 10)
    server.say(GROUP, BARTEK, "Ja jestem", NOW + 12)
    item = check(store, server, NOW + 12 + QUIET)[0]
    assert item["why"] == "group" and item["group"] and not item["dm"]
    assert item["place"] == "grupa „Rodzina”" and item["from"] == "Anna Nowak, Bartek"
    assert "Anna Nowak: Obiad" in item["body"] and "Bartek: Ja jestem" in item["body"]


def test_the_channel_falls_back_to_the_puppet_prefix(store, server):
    server.rooms[DM]["state"] = [event for event in server.rooms[DM]["state"] if event["type"] != "m.bridge"]
    baseline(store, server)
    server.say(DM, ANNA, "bez stanu mostu", NOW + 10)
    assert check(store, server, NOW + 10 + QUIET)[0]["channel"] == "WhatsApp"


def test_limit_holds_the_rest_for_the_next_check(store, server):
    baseline(store, server)
    server.say(DM, ANNA, "a", NOW + 1)
    server.say(GROUP, BARTEK, "b", NOW + 2)
    server.say(IG, IGOR, "c", NOW + 3)
    later = NOW + 3 + QUIET
    candidates, key, cursor = matrix(server, later).inbox({}, store, limit=2)
    assert [c["room_id"] for c in candidates] == [DM, GROUP] and store.get_meta("source_backlog:matrix")
    store.set_meta(key, cursor)
    assert [c["room_id"] for c in check(store, server, later)] == [IG]


def test_messages_older_than_max_age_are_not_new(store, server):
    baseline(store, server)
    server.say(DM, ANNA, "z importu historii mostu", NOW - 7 * 86400)
    assert check(store, server, NOW + QUIET) == []


def test_the_token_file_must_be_private(tmp_path):
    token = tmp_path / "matrix.token"
    token.write_text("syt_synthetic_token\n")
    os.chmod(token, 0o644)
    with pytest.raises(MatrixError, match="chmod 600"):
        read_token({"token_file": str(token)})
    os.chmod(token, 0o600)
    assert read_token({"token_file": str(token)}) == "syt_synthetic_token"
    with pytest.raises(MatrixError, match="token"):
        read_token({"token_file": str(tmp_path / "missing")})


def test_errors_never_carry_the_token(tmp_path):
    from hermes_auto_matrix import Http
    http = Http("http://127.0.0.1:9", "syt_secret_value")  # nothing listens on the discard port
    with pytest.raises(MatrixError) as error:
        http.get("/_matrix/client/v3/account/whoami")
    assert "syt_secret_value" not in str(error.value)


def test_sync_never_marks_the_user_online(store, server):
    # /sync without set_presence marks the account online (a write the bridges and other users can see).
    baseline(store, server)
    server.say(DM, ANNA, "Czy możesz jutro?", NOW + 10)
    check(store, server, NOW + 10 + QUIET)
    syncs = [params for path, params in server.calls if path.endswith("/sync")]
    assert len(syncs) == 2 and all(params.get("set_presence") == "offline" for params in syncs)


def test_a_late_bridged_message_with_an_earlier_timestamp_still_counts(store, server):
    # A bridge delivering a backlog (the phone was offline) keeps each message's own time, so a message can
    # arrive after a later one was handed over: its event id is new, its timestamp is not.
    baseline(store, server)
    server.say(GROUP, BARTEK, "Kto jutro przyjdzie?", NOW + 100)
    assert [c["from"] for c in check(store, server, NOW + 100 + QUIET)] == ["Bartek"]
    server.say(GROUP, ANNA, "Łukasz, oddasz mi klucze?", NOW + 50)
    later = check(store, server, NOW + 200 + QUIET)
    assert [(c["from"], c["snippet"]) for c in later] == [("Anna Nowak", "Łukasz, oddasz mi klucze?")]
    assert check(store, server, NOW + 400 + QUIET) == []  # handed once


def test_http_follows_no_redirect_and_errors_carry_no_server_text():
    # A redirect would carry the Authorization header to another origin; an error's errcode is the server's text.
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from hermes_auto_matrix import Http
    seen = []

    class Other(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"user_id": "@x:example.test"}')

        def log_message(self, *args):
            pass

    other = HTTPServer(("127.0.0.1", 0), Other)

    class Home(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/redirect"):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{other.server_port}/steal")
                self.end_headers()
                return
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"errcode": "echo syt_secret_value"}).encode())

        def log_message(self, *args):
            pass

    home = HTTPServer(("127.0.0.1", 0), Home)
    for server_ in (home, other):
        threading.Thread(target=server_.serve_forever, daemon=True).start()
    try:
        http = Http(f"http://127.0.0.1:{home.server_port}", "syt_secret_value")
        with pytest.raises(MatrixError, match="HTTP 302"):
            http.get("/redirect")
        assert seen == []
        with pytest.raises(MatrixError) as error:
            http.get("/_matrix/client/v3/account/whoami")
        assert "HTTP 401" in str(error.value) and "syt_secret_value" not in str(error.value)
    finally:
        home.shutdown()
        other.shutdown()


def test_history_lines_show_the_chat_for_the_first_turn(server):
    server.say(DM, ANNA, "Czy możesz jutro?", NOW)
    server.say(DM, ME, "Mogę", NOW + 60)
    lines = history_lines(matrix(server, NOW + 120), DM, 30)
    assert lines[-2].endswith("Anna Nowak: Czy możesz jutro?") and lines[-1].endswith("Łukasz: Mogę")
    with pytest.raises(MatrixError):
        history_lines(matrix(server, NOW), "#alias:example.test", 30)
    assert all(path.startswith("/_matrix/client/v3/") for path, _ in server.calls)


# ── Sorter path ──────────────────────────────────────────────────────────────

@pytest.fixture
def config():
    return {**defaults(), "language": "Polish", "sources": {"matrix": {"enabled": True}},
            "triage": {"enabled": True, "brief_times": ["07:45", "16:30"]}}


def dm_item(store, server):
    baseline(store, server)
    server.say(DM, ANNA, "Czy możesz jutro o 10?", NOW + 10)
    return check(store, server, NOW + 10 + QUIET)[0]


def test_sorted_like_other_message_sources_and_never_written_to():
    assert "matrix" in cli.MAIL_LIKE and triage.deny_profile({"kind": "matrix"}) is True
    assert not hasattr(Source, "mark_read") and not hasattr(Source, "finish")  # a receipt would reach the sender


def test_session_shows_the_messenger_and_may_read_its_own_room(store, server, config):
    item = dm_item(store, server)
    fake = Fake(replies=[[{"i": 0, "decision": "session", "reason": "pyta o termin", "summary": "Anna pyta"}]])
    triage.triage_mail(store, config, fake, "matrix", ME, [item], "k", {"since": "1"})
    system, user = fake.prompts[0]
    assert "komunikator" in system and user[0]["channel"] == "WhatsApp" and user[0]["where"] == "WhatsApp · DM"
    job = store.rows()[0]
    assert job["source"] == "matrix" and deny_mode(job) is True and third_party(job)
    assert session_title(job) == "WhatsApp · Anna Nowak"
    prompt = prompt_for(job, config)
    assert f"hermes-auto-matrix history {DM} --limit 30" in prompt and "NIGDY sam nie odpowiadaj" in prompt
    card = session_card(json.loads(job["payload"]), "pyta o termin", config)
    assert card.splitlines()[0] == "WhatsApp · Anna Nowak napisał do Ciebie"
    assert "WhatsApp · DM" in card and "Czy możesz" not in card


def test_a_later_message_in_the_room_follows_into_the_session(store, server, config):
    item = dm_item(store, server)
    triage.triage_mail(store, config, Fake(replies=[[{"i": 0, "decision": "session", "reason": "", "summary": ""}]]),
                       "matrix", ME, [item], "k", {"since": "1"})
    session = store.rows()[0]["session_id"]
    server.say(DM, ANNA, "Albo o 11?", NOW + 500)
    later = check(store, server, NOW + 500 + QUIET)
    report = triage.triage_mail(store, config, Fake(), "matrix", ME, later, "k", {"since": "2"})
    assert [(decision, detail) for decision, _, detail in report] == [("followup", session)]


def test_brief_lists_the_messenger_and_marks_nothing_read(store, server, config):
    item = dm_item(store, server)
    fake = Fake(replies=[[{"i": 0, "decision": "brief", "reason": "", "summary": "Anna wysłała zdjęcie"}]])
    triage.triage_mail(store, config, fake, "matrix", ME, [item], "k", {"since": "1"})
    rows = [dict(row) for row in store.db.execute("SELECT * FROM triage_items")]
    text = triage.render_brief(rows, config)
    assert "## Komunikatory (1)" in text and "WhatsApp" in text and "Anna wysłała zdjęcie" in text
    assert triage.flush_read_marks(store, config, {"matrix": Source}) == []



def test_chats_stay_on_the_private_account_under_the_work_routes(store, server):
    from test_one_session import ALL, FIRST_TURN, ROUTES
    item = dm_item(store, server)
    routed = {**defaults(), "routes": ROUTES, "first_turn": FIRST_TURN}
    assert triage.model_route(routed, "matrix", item) == {} and triage.model_route(routed, "imessage", item) == {}
    assert triage.read_scope(routed, item, "matrix") == (frozenset(ALL), True)


def test_the_homeserver_must_be_configured():
    with pytest.raises(MatrixError, match="homeserver"):
        Matrix(Source.defaults())


# ── Push: the listener next to the homeserver ────────────────────────────────

from hermes_auto import listening  # noqa: E402
from hermes_auto_matrix.listener import MatrixListener  # noqa: E402


class Sink:
    """Records what the listener publishes; `fail` makes it refuse like an unreachable relay."""

    def __init__(self):
        self.emitted, self.beats, self.fail = [], [], False

    def emit(self, source, hint=None, wake_after=None):
        if self.fail:
            raise RuntimeError("Relay did not accept the event")
        self.emitted.append((source, hint, wake_after))

    def beat(self, sources, connected):
        self.beats.append((tuple(sources), connected))

    def pump(self):
        pass


def listener(tmp_path, server, sink, now=NOW, **settings):
    return MatrixListener(tmp_path, {**settings}, http=server, sink=sink, clock=lambda: now)


def test_the_listener_publishes_when_someone_else_writes_and_never_the_text(tmp_path, server):
    sink = Sink()
    watch = listener(tmp_path, server, sink)
    server.say(DM, ANNA, "sprzed startu", NOW - 60)
    assert watch.check()  # the baseline after a start: one check for whatever came while it was down
    syncs = [params for path, params in server.calls if path.endswith("/sync")]
    assert json.loads(syncs[0]["filter"])["room"]["timeline"]["limit"] == 0
    server.say(DM, ME, "moja odpowiedź", NOW + 1)
    server.say(MGMT, "@whatsappbot:example.test", "Bridge status: connected", NOW + 2)
    assert not watch.check()  # own message and the bridge bot: nothing to publish
    server.say(DM, ANNA, "Czy możesz jutro o 10?", NOW + 3)
    assert watch.check()
    assert sink.emitted == [("matrix", None, None), ("matrix", None, None)]  # the source name only, no text
    syncs = [params for path, params in server.calls if path.endswith("/sync")]
    assert all(params["set_presence"] == "offline" for params in syncs)
    assert int(syncs[-1]["timeout"]) == 30_000  # a long poll, not a busy loop
    assert sink.beats and sink.beats[0] == (("matrix",), True)


def test_a_refused_publish_keeps_the_position_so_the_message_publishes_again(tmp_path, server):
    sink = Sink()
    watch = listener(tmp_path, server, sink)
    watch.check()
    server.say(DM, ANNA, "halo", NOW + 3)
    sink.fail = True
    with pytest.raises(RuntimeError):
        watch.check()
    sink.fail = False
    assert watch.check() and len(sink.emitted) == 2


def test_skipped_rooms_and_old_backfill_do_not_publish(tmp_path, server):
    sink = Sink()
    watch = listener(tmp_path, server, sink, skip_rooms=[GROUP])
    watch.check()
    server.say(GROUP, ANNA, "w pominiętej grupie", NOW + 1)
    server.say(DM, ANNA, "historia sprzed dwóch dni", NOW - 2 * 86400)
    assert not watch.check() and len(sink.emitted) == 1


def test_a_nudge_makes_the_mac_check_matrix_once_the_room_went_quiet(store, server, tmp_path):
    # Relay -> LocalSink on the Mac writes matrix-listener.json; the source is due after `quiet_seconds`
    # without waiting for its polling interval, and the burst is ready by then.
    baseline(store, server)
    settings = {"enabled": True, "poll_seconds": 0}
    assert not Source().due(settings, store, NOW, NOW + 5)
    server.say(DM, ANNA, "Czy możesz jutro o 10?", NOW + 10)
    listening.write_state(tmp_path, "matrix", {"nudge_at": NOW + 11})
    assert not Source().due(settings, store, NOW, NOW + 11 + QUIET - 1)  # still quiet-waiting
    assert Source().due(settings, store, NOW, NOW + 11 + QUIET)
    assert [c["room_id"] for c in check(store, server, NOW + 11 + QUIET)] == [DM]
    assert not Source().due(settings, store, NOW + 11 + QUIET, NOW + 200)  # that nudge is used up


def test_polling_stays_as_the_fallback(store):
    assert Source().due({"poll_seconds": 60}, store, NOW, NOW + 60)
    assert not Source().due({"poll_seconds": 60}, store, NOW, NOW + 30)


def test_chats_wait_at_most_30_seconds_of_quiet():
    assert Source.defaults()["quiet_seconds"] <= 30
