"""Send one mail through the local Proton Bridge SMTP and print a receipt Hermes Auto can trace.

`hermes-auto-proton-send --to a@b.test --subject "..." --body-file body.txt` prints exactly
`{"status": "sent", "id": "<Message-ID>", "threadId": "<thread root>"}` after the SMTP server accepted
the message (with `"refused": [...]` when Bridge turned down only some recipients), and nothing else
on stdout. The bridge's `find_session` recognises this
command and receipt (like `google_api.py gmail send`), so a reply to the mail goes back to the Hermes
session that sent it. Bridge files the copy in Sent itself (keeping this Message-ID), so the sorter's
Proton adapter finds the same id in the reply's References.

Credentials, host and certificate come from `sources.proton` in the Hermes Auto config; the password is
read with its `password_command` and never printed. A failure exits non-zero without a receipt.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import smtplib
import ssl
import subprocess
import sys
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
from pathlib import Path

from . import Source, message_ids


def build_message(args, sender):
    message = EmailMessage()
    message["From"] = args.from_ or sender
    message["To"] = ", ".join(args.to)
    if args.cc:
        message["Cc"] = ", ".join(args.cc)
    message["Subject"] = args.subject
    message["Date"] = formatdate(localtime=True)
    domain = parseaddr(str(message["From"]))[1].rpartition("@")[2] or None
    message["Message-ID"] = make_msgid(domain=domain)
    parents = message_ids(args.in_reply_to)
    references = [ref for value in args.references for ref in message_ids(value)] or parents
    if parents:
        message["In-Reply-To"] = parents[-1]
    if references:
        message["References"] = " ".join(dict.fromkeys(references))
    message.set_content(Path(args.body_file).expanduser().read_text())
    for attachment in args.attach:
        path = Path(attachment).expanduser()
        kind, _ = mimetypes.guess_type(path.name)
        maintype, _, subtype = (kind or "application/octet-stream").partition("/")
        message.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name)
    return message, (references or [message["Message-ID"]])[0]


def settings_from(home):
    raw = json.loads((home / "config.json").read_text()).get("sources", {}).get("proton", {})
    settings = {**Source.defaults(), "smtp_port": 1025, **raw}
    if settings["host"] not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("Proton Bridge must be local")
    if not settings.get("username") or not settings.get("password_command"):
        raise ValueError("Configure sources.proton username and password_command (Bridge credentials)")
    return settings


def send(settings, message, smtp=smtplib.SMTP):
    context = ssl.create_default_context(cafile=str(Path(settings["certificate"]).expanduser()))
    context.check_hostname = False  # The explicit Bridge certificate's CN may not be the loopback address.
    password = subprocess.run(settings["password_command"], capture_output=True, text=True, timeout=15,
                              check=True).stdout.strip()
    with smtp(settings["host"], int(settings["smtp_port"]), timeout=60) as client:
        client.starttls(context=context)
        client.login(settings["username"], password)
        # All recipients refused raises; a partial refusal still sent the mail, so it gets a receipt.
        return sorted(client.send_message(message))


def main(argv=None, smtp=smtplib.SMTP):
    parser = argparse.ArgumentParser(description="Send one mail through Proton Bridge with a traceable receipt")
    parser.add_argument("--home", type=Path, default=Path.home() / ".config/hermes-auto")
    parser.add_argument("--from", dest="from_", default="", help="sender address (default: Bridge username)")
    parser.add_argument("--to", action="append", required=True)
    parser.add_argument("--cc", action="append", default=[])
    parser.add_argument("--subject", required=True)
    parser.add_argument("--body-file", required=True, help="plain-text body (UTF-8)")
    parser.add_argument("--attach", action="append", default=[])
    parser.add_argument("--in-reply-to", default="", help="Message-ID of the mail this answers")
    parser.add_argument("--references", action="append", default=[], help="thread Message-IDs, oldest first")
    args = parser.parse_args(argv)
    settings = settings_from(args.home.expanduser())
    message, thread = build_message(args, settings["username"])
    refused = send(settings, message, smtp=smtp)
    receipt = {"status": "sent", "id": message["Message-ID"], "threadId": thread}
    print(json.dumps({**receipt, "refused": refused} if refused else receipt))
    return 0


if __name__ == "__main__":
    sys.exit(main())
