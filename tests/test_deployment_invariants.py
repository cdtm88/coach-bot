"""Two things that only break once the code is installed rather than imported.

Both were found on a live deploy rather than here, which is the reason this file
exists. Neither is exotic: one is a path resolved relative to the source tree,
the other is a secret in a URL. What they share is that the test suite runs from
a checkout with a chatty logger nobody reads, so both were invisible.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

import pytest

from coach import migrate
from coach.llm import router
from coach.runtime import transport

# SEC-01. This was the live bot token until 5 August 2026, committed here on
# 2 August in #20 as a redaction fixture, in a public repository. It was scraped
# and used to send spam into the athlete's chat three days later. That token is
# burned and stays burned. What remains is the lesson that a fixture is not a
# safe place for a real one, because nothing about this file's purpose made it
# look like a secret store.
#
# The shape is what matters and the value never did: `_TOKEN_IN_URL` matches
# `/bot\d{4,}:[A-Za-z0-9_-]{20,}`, so this exercises the redactor exactly as the
# real one did. `test_no_real_credential_is_committed_to_this_repository` below
# is the guard that keeps the next one out.
TOKEN = "1234567890:AAnot-a-real-token-do-not-paste-a-real-one-here"


def test_an_empty_migrations_directory_is_an_error(tmp_path: Path) -> None:
    """The failure this replaces was silent, which is what made it expensive.

    `sorted(directory.glob("*.sql"))` on a directory that does not exist returns
    `[]`. Nothing is pending, so `coach-migrate` logs `schema up to date`, exits
    0, and `depends_on: service_completed_successfully` lets every other service
    start against a completely empty database. They then fail one query later
    with `relation "messages" does not exist`, pointing at the schema rather
    than at the boot step that was supposed to create it.
    """
    with pytest.raises(migrate.MigrationsNotFound, match="COACH_MIGRATIONS_DIR"):
        migrate.discover(tmp_path / "nowhere")


def test_the_migrations_directory_can_be_pointed_somewhere_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Because `parents[2]` is the repository root only from a checkout.

    Installed into site-packages — which is how the image ships, and the only
    way copying a virtualenv between build stages can work — it resolves into
    the interpreter's `lib` directory. The image sets this variable; without it
    there is no correct answer available to the package itself.
    """
    (tmp_path / "001_x.sql").write_text("select 1")
    monkeypatch.setenv("COACH_MIGRATIONS_DIR", str(tmp_path))

    assert migrate.discover() == [tmp_path / "001_x.sql"]


def test_the_real_migrations_are_still_found_without_the_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("COACH_MIGRATIONS_DIR", raising=False)

    found = migrate.discover()

    assert len(found) >= 13
    assert found[0].name.startswith("001")


def test_the_bot_token_never_reaches_a_log_line(caplog: pytest.LogCaptureFixture) -> None:
    """Telegram puts the token in the URL path, so httpx logs it on every poll.

    `INFO:httpx:HTTP Request: POST https://api.telegram.org/bot<token>/getUpdates`
    — once per long poll, forever, at the default level. The transport was
    already careful to keep the token out of its own exception messages, which
    bought nothing against a line the library writes itself.
    """
    transport.Telegram(token=TOKEN, client=object())  # type: ignore[arg-type]

    with caplog.at_level(logging.INFO):
        logging.getLogger("httpx").info(
            'HTTP Request: POST https://api.telegram.org/bot%s/getUpdates "HTTP/1.1 200 OK"',
            TOKEN,
        )

    assert TOKEN not in caplog.text
    assert "<token redacted>" in caplog.text


def test_redaction_is_installed_by_construction_not_by_a_caller() -> None:
    """A filter an entry point has to remember to install is a filter that leaks.

    The constructor is the only way to get something that talks to Telegram, so
    it is the only place that cannot be skipped.
    """
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).filters = [
            f for f in logging.getLogger(name).filters if f is not transport._REDACTOR
        ]

    transport.Telegram(token=TOKEN, client=object())  # type: ignore[arg-type]

    assert transport._REDACTOR in logging.getLogger("httpx").filters
    assert transport._REDACTOR in logging.getLogger("httpcore").filters


def test_the_redactor_leaves_an_ordinary_url_alone(caplog: pytest.LogCaptureFixture) -> None:
    """Silencing httpx outright would hide the intervals.icu request log too.

    That one is genuinely useful and carries no secret — its auth is in a
    header — so the filter has to be narrow enough to leave it intact.
    """
    transport.Telegram(token=TOKEN, client=object())  # type: ignore[arg-type]

    with caplog.at_level(logging.INFO):
        logging.getLogger("httpx").info(
            'HTTP Request: GET https://intervals.icu/api/v1/athlete/i1/activities "HTTP/1.1 200 OK"'
        )

    assert "intervals.icu/api/v1/athlete/i1/activities" in caplog.text


def test_every_routable_purpose_can_be_configured_from_the_deployment() -> None:
    """MODEL-02, which the code satisfied and the deployment did not.

    `docker-compose.yml` has no `env_file:`, so it forwards nothing it does not
    name: `${MODEL_CHAT}` is only substituted into a value that some
    `environment:` block declares. Putting MODEL_CHAT in .env and restarting
    therefore changed nothing, and looked precisely like a setting that had been
    applied — the container came up, the coach replied, and it replied from the
    default model.

    A third failure in the family this file is for: correct code, installed in a
    way that cannot reach it. The suite could not see it because the suite reads
    the environment directly.

    Asserted against `router.PURPOSES` rather than a list, so a purpose added
    later fails here instead of being quietly unconfigurable in production. The
    Sunday review's purpose was added without this and would have been the next
    one to go missing.
    """
    compose = (Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text()

    missing = [
        key
        for purpose in router.PURPOSES
        for key in (f"MODEL_{purpose.upper()}", f"EFFORT_{purpose.upper()}")
        if f"{key}: ${{{key}" not in compose
    ]

    assert not missing, f"not passed through to the containers: {missing}"


# The credential shapes worth refusing. Each is anchored on a vendor prefix or a
# fixed length rather than on entropy, because an entropy threshold over a
# repository of prose and SQL is all false positives and gets deleted within a
# week.
_CREDENTIAL_SHAPES = {
    "Telegram bot token": re.compile(r"\d{8,10}:[A-Za-z0-9_-]{35}"),
    "Anthropic API key": re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    "OpenAI-style key": re.compile(r"\bsk-[A-Za-z0-9]{32,}"),
    "AWS access key id": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36}\b"),
    "private key block": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "password in a URI": re.compile(r"://[^/\s:@]+:(?!CHANGEME)[^/\s:@]{8,}@"),
}

# A fixture has to say it is one, inside the matched text itself. A real
# credential cannot contain these, which is what makes the check one sided:
# there is no way to silence it except by not committing the secret.
#
# `PASSWORD` and `YOUR` are here for the documented URI templates, which write
# the shape out in full so the reader can see where the value goes. They are
# safe markers for the same reason as the rest: an uppercase English word is not
# what a generated secret looks like.
_DECLARED_FAKE = (
    "not-a-real",
    "CHANGEME",
    "REDACTED",
    "example",
    "EXAMPLE",
    "placeholder",
    "PASSWORD",
    "YOUR",
)


def test_no_real_credential_is_committed_to_this_repository() -> None:
    """SEC-01, as a test rather than as a rule nobody runs.

    The rule already existed — `.gitignore` keeps `.env` out and the transport
    goes to some length to keep the token out of a log line — and none of it
    helped, because the token that leaked was pasted into a *test fixture* on
    2 August 2026 and the repository is public. It was scraped and used to send
    spam into the athlete's Telegram chat on 5 August. Every guard in the
    codebase was pointed at the running system; nothing was pointed at the
    checkout.

    Scoped to `git ls-files`, so it asks the only question that matters — what
    is actually published — rather than what happens to be on this disk. It
    cannot see history: the 2 August token stays in the log of a public
    repository forever, which is why the response to this was rotation and not
    a rewrite.
    """
    root = Path(__file__).resolve().parents[1]
    listed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        capture_output=True,
        text=True,
        check=True,
    )
    tracked = [p for p in listed.stdout.split("\0") if p]
    assert len(tracked) > 100, f"git ls-files returned {len(tracked)} paths, which is not this repo"

    findings: list[str] = []
    for relative in tracked:
        path = root / relative
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # binary or gone; neither can be reviewed as text
        for label, pattern in _CREDENTIAL_SHAPES.items():
            for match in pattern.finditer(content):
                hit = match.group(0)
                if any(marker in hit for marker in _DECLARED_FAKE):
                    continue
                # `${POSTGRES_PASSWORD}` is the name of a secret, not one. This
                # holds for every shape above, so it is checked once here rather
                # than written into each pattern.
                if "$" in hit:
                    continue
                line = content.count("\n", 0, match.start()) + 1
                findings.append(f"{relative}:{line}: looks like a {label}")

    assert not findings, "credential-shaped strings are tracked by git:\n" + "\n".join(findings)
