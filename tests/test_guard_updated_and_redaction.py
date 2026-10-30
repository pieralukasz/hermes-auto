"""Guard 3.0.3: `gh search prs/issues --updated` with a checked date filter, and credentials in URLs of a read's
output (and of a raw skill read) replaced by REDACTED before the model sees them. Synthetic owners and secrets."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_auto import guard_plugin as plugin
from hermes_auto.guard_plugin import policy
from hermes_auto.guard_plugin.policy import read_only_command

SECRET = "SYNTHETICsecret0123"


# ── gh search --updated ──────────────────────────────────────────────────────

@pytest.mark.parametrize("value", [
    "'>=2026-10-01'", "'>2026-10-01'", "'<2026-10-01'", "'<=2026-10-01'", "2026-10-01", "2026-10-01..2026-10-20",
    "'*..2026-10-20'", "'2026-10-01..*'", "2026-10-01T08:30:00Z", "'>=2026-10-01T08:30:00+02:00'",
])
@pytest.mark.parametrize("group", ["prs", "issues"])
def test_search_takes_a_date_filter_for_updated(group, value):
    command = f"gh search {group} --owner acme --author someone --updated {value} --limit 20 --json number,url"
    assert read_only_command(command, "research") is True


@pytest.mark.parametrize("token", [
    "--updated=2026-10-01..2026-10-20", "--updated='>=2026-10-01'", "'--updated=>=2026-10-01'",
    "--updated='<2026-10-01'",
])
def test_updated_also_takes_the_attached_form(token):
    assert read_only_command(f"gh search prs --owner acme {token} showcase", "research") is True


@pytest.mark.parametrize("value", [
    "--json", "-L", "'--json=x'", "''", "yesterday", "2026-1-1", "'2026-10-01 repo:other/x'",
    "'>=2026-10-01 is:private'", "'2026-10-01;id'", "'$(id)'", "'`id`'", "'2026-10-01|x'", "'>='", "'..2026-10-01'",
    "'2026-10-01..'", "'>>2026-10-01'", "'2026-10-01\\nx'", "https://evil.test/2026-10-01",
])
def test_updated_refuses_anything_but_a_date_filter(value):
    assert read_only_command(f"gh search prs --owner acme --updated {value}", "research") is False
    assert read_only_command(f"gh search prs --owner acme --updated={value}", "research") is False


def test_updated_needs_a_value_and_stays_on_search():
    assert read_only_command("gh search prs --owner acme --updated", "research") is False
    assert read_only_command("gh search prs --owner acme --updated --limit 5", "research") is False
    for command in ("gh search code --updated 2026-10-01 x", "gh pr list --repo acme/app --updated 2026-10-01",
                    "gh search commits --updated 2026-10-01 x"):
        assert read_only_command(command, "research") is False
    # Outside the research families gh is no read at all.
    assert read_only_command("gh search prs --updated 2026-10-01 x", "mail") is False


def test_an_unquoted_comparison_is_refused_with_a_hint(monkeypatch):
    monkeypatch.setattr(policy.subprocess, "run", lambda *a, **k: pytest.fail("must not run"))
    for command in ("gh search prs --owner acme --updated >=2026-10-01",
                    'gh search prs --owner acme --updated ">=2026-10-01"'):
        result = json.loads(policy.run_read(command, "research"))
        assert result["skipped"] is True and "single quotes" in result["note"] and "'>=2026-10-01'" in result["note"]
    # Other refusals keep the plain note.
    assert "single quotes" not in json.loads(policy.run_read("git log -5", "research"))["note"]


def test_the_quoted_comparison_runs_as_one_argument(monkeypatch):
    seen = {}

    def run(argv, **kwargs):
        seen["argv"] = argv
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")
    monkeypatch.setattr(policy.subprocess, "run", run)
    policy.run_read("gh search prs --owner acme --updated '>=2026-10-01' --json number", "research")
    assert seen["argv"] == ["gh", "search", "prs", "--owner", "acme", "--updated", ">=2026-10-01", "--json", "number"]


# ── Redaction of credentials in URLs ─────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    (f"https://raw.githubusercontent.com/acme/app/main/src/a.ts?token={SECRET}",
     "https://raw.githubusercontent.com/acme/app/main/src/a.ts?token=REDACTED"),
    (f"https://files.example.test/b/k.png?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=AKIA%2F{SECRET}"
     f"&X-Amz-Date=20261030T000000Z&X-Amz-Expires=300&X-Amz-Security-Token={SECRET}&X-Amz-Signature={SECRET}",
     "https://files.example.test/b/k.png?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=REDACTED"
     "&X-Amz-Date=20261030T000000Z&X-Amz-Expires=300&X-Amz-Security-Token=REDACTED&X-Amz-Signature=REDACTED"),
    (f"https://uploads.example.test/a/b.png?signature={SECRET}", "https://uploads.example.test/a/b.png?signature=REDACTED"),
    (f"https://blob.example.test/c?sv=2024&sig={SECRET}&se=2026", "https://blob.example.test/c?sv=2024&sig=REDACTED&se=2026"),
    (f"https://maps.example.test/x?center=1,2&key={SECRET}", "https://maps.example.test/x?center=1,2&key=REDACTED"),
    (f"https://shop.example.test/cart/1:1?ACCESS_TOKEN={SECRET}", "https://shop.example.test/cart/1:1?ACCESS_TOKEN=REDACTED"),
    (f"https://id.example.test/cb?code=1&client_secret={SECRET}", "https://id.example.test/cb?code=1&client_secret=REDACTED"),
    (f"https://app.example.test/#access_token={SECRET}&state=1", "https://app.example.test/#access_token=REDACTED&state=1"),
    (f"https://x-access-token:{SECRET}@github.com/acme/app.git", "https://x-access-token:REDACTED@github.com/acme/app.git"),
    (f'<a href="https://x.example.test/p?a=1&amp;signature={SECRET}&amp;b=2">',
     '<a href="https://x.example.test/p?a=1&amp;signature=REDACTED&amp;b=2">'),
    (f"Linkhttps://files.example.test/a?token={SECRET}", "Linkhttps://files.example.test/a?token=REDACTED"),
    (f'{{"u":"https:\\/\\/x.test\\/?token=\\/{SECRET}\\/x\\u0026a=1"}}',
     '{"u":"https:\\/\\/x.test\\/?token=REDACTED\\u0026a=1"}'),
    (f"https://x.test/?token=AAA%26{SECRET}&a=1", "https://x.test/?token=REDACTED&a=1"),
    (f"https://x.test/?token=AAA;{SECRET}&a=1", "https://x.test/?token=REDACTED&a=1"),
    (f"https://x.test/?auth_token={SECRET}&jwt={SECRET}", "https://x.test/?auth_token=REDACTED&jwt=REDACTED"),
    (f"https://{SECRET}0123456789@github.com/acme/app.git", "https://REDACTED@github.com/acme/app.git"),
    (f"https://www.redirect.test/url?q=https%3A%2F%2Fx.test%2Fa%3Ftoken%3D{SECRET}%26sa%3DD&usg=1",
     "https://www.redirect.test/url?q=https%3A%2F%2Fx.test%2Fa%3Ftoken%3DREDACTED%26sa%3DD&usg=1"),
])
def test_credentials_in_urls_are_redacted_and_the_link_stays_recognisable(text, expected):
    assert policy.redact_urls(text) == expected
    assert policy.redact_urls(expected) == expected  # idempotent


@pytest.mark.parametrize("text", [
    "token=abc outside any URL, key: value, signature=keep",
    "https://x.example.test/search?keyword=a&monkey=b&tokens=c&ref=main",
    "https://host.example.test:8443/path?a=1",
    "ssh://git@github.com/acme/app.git and https://me@example.test/x",
    "https://x.example.test/p?key=",
    "",
])
def test_text_without_url_credentials_is_unchanged(text):
    assert policy.redact_urls(text) == text


def test_gh_api_json_output_reaches_the_model_without_the_token(monkeypatch):
    body = json.dumps([{"name": "a.ts", "path": "src/a.ts", "sha": "0" * 40,
                        "url": "https://api.github.com/repos/acme/app/contents/src/a.ts?ref=main",
                        "html_url": "https://github.com/acme/app/blob/main/src/a.ts",
                        "download_url": f"https://raw.githubusercontent.com/acme/app/main/src/a.ts?token={SECRET}",
                        "_links": {"self": "https://api.github.com/repos/acme/app/contents/src/a.ts?ref=main"}}])
    # Go's encoder escapes `&` and may escape `/`; the same link in that form.
    escaped = (f'{{"download_url":"https:\\/\\/raw.githubusercontent.com\\/acme\\/app\\/main\\/b.ts?ref=main'
               f'\\u0026token={SECRET}"}}')
    monkeypatch.setattr(policy.subprocess, "run", lambda argv, **kw: SimpleNamespace(
        returncode=0, stdout=body + "\n" + escaped, stderr=f"warning: see https://x.example.test/l?sig={SECRET}"))
    result = json.loads(policy.run_read("gh api repos/acme/app/contents/src", "research"))
    assert SECRET not in json.dumps(result) and result["exit_code"] == 0
    first = json.loads(result["output"].split("\n")[0])
    assert first[0]["download_url"] == "https://raw.githubusercontent.com/acme/app/main/src/a.ts?token=REDACTED"
    assert first[0]["url"] == "https://api.github.com/repos/acme/app/contents/src/a.ts?ref=main"
    assert "\\u0026token=REDACTED" in result["output"] and "?sig=REDACTED" in result["output"]


def test_redaction_happens_before_the_output_is_cut(monkeypatch):
    url = f"https://raw.githubusercontent.com/acme/app/main/a.ts?token={SECRET}"
    padding = "x" * (policy.OUTPUT_LIMIT - len(url) + 8)  # the cut falls inside the secret
    monkeypatch.setattr(policy.subprocess, "run", lambda argv, **kw: SimpleNamespace(
        returncode=0, stdout=padding + url + "\n" + "y" * 100, stderr=""))
    output = json.loads(policy.run_read("gh api repos/acme/app/contents/a.ts", "research"))["output"]
    assert SECRET[:6] not in output and "truncated" in output


def test_redaction_stays_fast_and_bounded_on_large_or_hostile_output(monkeypatch):
    import time
    hostile = "a." * 200_000 + "a+" * 200_000 + "a:" * 200_000  # no `://`: an unbounded scheme scan is quadratic
    started = time.monotonic()
    assert policy.redact_urls(hostile) == hostile
    huge = (f"https://raw.githubusercontent.com/acme/app/main/a.ts?token={SECRET}\n") * 200_000  # ~15 MB
    monkeypatch.setattr(policy.subprocess, "run", lambda argv, **kw: SimpleNamespace(
        returncode=0, stdout=huge, stderr=""))
    output = json.loads(policy.run_read("gh api repos/acme/app/contents/a.ts", "research"))["output"]
    assert time.monotonic() - started < 5
    assert SECRET not in output and output.endswith("narrow the query]")
    assert len(output) < policy.OUTPUT_LIMIT + 200


def test_output_shortened_only_by_redaction_is_not_marked_truncated(monkeypatch):
    long_secret = "S" * 400
    text = "x" * (policy.OUTPUT_LIMIT - 100) + f"\nhttps://files.example.test/a?sig={long_secret}\n"
    monkeypatch.setattr(policy.subprocess, "run", lambda argv, **kw: SimpleNamespace(
        returncode=0, stdout=text, stderr=""))
    output = json.loads(policy.run_read("gh api repos/acme/app/contents/a.ts", "research"))["output"]
    assert output.endswith("?sig=REDACTED\n") and "truncated" not in output


def test_a_raw_skill_read_is_redacted_too(tmp_path):
    skill = tmp_path / "skills" / "demo"
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text(f"Feed: https://shop.example.test/feed?token={SECRET}\n", encoding="utf-8")
    (skill / "references" / "a.md").write_text(f"https://maps.example.test/x?key={SECRET}\n", encoding="utf-8")
    main = json.loads(policy.read_skill_text(skill / "SKILL.md"))
    ref = json.loads(policy.read_skill_text(skill / "SKILL.md", "references/a.md"))
    assert main["content"] == "Feed: https://shop.example.test/feed?token=REDACTED\n"
    assert ref["content"] == "https://maps.example.test/x?key=REDACTED\n"


def test_a_skill_url_at_the_read_bound_is_dropped_not_shown_in_part(tmp_path):
    skill = tmp_path / "skills" / "demo"
    skill.mkdir(parents=True)
    # Secrets long enough that redaction pulls text from past the read bound into view.
    filler = "".join(f"https://files.example.test/{n}?sig={'S' * 300}\n" for n in range(40))
    head = filler + "y" * (policy.SKILL_TEXT_LIMIT + policy.REDACT_MARGIN - len(filler) - 30)
    (skill / "SKILL.md").write_text(head + f"https://user:{SECRET}{SECRET}@git.example.test/r.git\n" + "z" * 50,
                                    encoding="utf-8")
    content = json.loads(policy.read_skill_text(skill / "SKILL.md"))["content"]
    assert "SSSS" not in content and SECRET[:5] not in content and "truncated" in content


def test_skills_list_output_is_redacted(monkeypatch):
    import sys
    fake = SimpleNamespace(skills_list=lambda category=None, task_id=None: json.dumps(
        {"skills": [{"name": "demo", "description": f"feed https://shop.example.test/f?token={SECRET}"}]}))
    monkeypatch.setitem(sys.modules, "tools", SimpleNamespace(skills_tool=fake))
    monkeypatch.setitem(sys.modules, "tools.skills_tool", fake)
    result = plugin.skills_list_tool({})
    assert SECRET not in result and "?token=REDACTED" in result


def test_guard_version_is_the_same_in_code_and_manifest():
    manifest = (Path(plugin.__file__).parent / "plugin.yaml").read_text()
    assert policy.GUARD_VERSION == "3.0.3" and f'version: "{policy.GUARD_VERSION}"' in manifest
