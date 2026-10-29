"""Source read retries: a network outage must not leave sources waiting out a long backoff after it ends."""

import time

import pytest

from hermes_auto import cli
from hermes_auto.config import defaults
from hermes_auto.store import Store

DNS = ("triage linear: Linear read failed (exit 5): \u2717 API request failed: Request to https://api.linear.app "
       "failed: failed to lookup address information: nodename nor servname provided, or not known")
MATRIX = "triage matrix: Matrix GET /_matrix/client/v3/account/whoami failed: URLError"
AUTH = "todoist: /opt/homebrew/bin/td failed (exit 1); check its authentication"


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path, initialize=True)
    yield result
    result.close()


@pytest.fixture
def config():
    names = ("linear", "matrix", "gmail", "todoist")
    return {**defaults(), "sources": {name: {"enabled": True, "poll_seconds": 1} for name in names}}


def retry(store, name):
    return store.get_meta(f"source_retry:{name}", None)


def fail(store, names, errors, times):
    for _ in range(times):
        cli.settle_checked(store, names, errors, time.time())


@pytest.mark.parametrize("error", [
    DNS, MATRIX,
    "triage linear: Linear read failed (exit 5): \u2717 API request failed: Request to https://api.linear.app "
    "failed: Network is unreachable (os error 51)",
    "triage linear: Linear read failed (exit 5): \u2717 API request failed: No route to host (os error 65)",
    "triage slack: HTTPSConnectionPool(host='slack.com', port=443): Max retries exceeded with url: /api/auth.test "
    "(Caused by NameResolutionError(\"Failed to resolve 'slack.com'\"))",
])
def test_network_errors_are_recognised(error):
    assert cli.network_error(error)


@pytest.mark.parametrize("error", [AUTH, "triage linear: Linear read failed (exit 1): Unauthorized"])
def test_other_errors_are_not_network_errors(error):
    assert not cli.network_error(error)


def test_network_failure_backs_off_at_most_five_minutes(store):
    fail(store, ["linear"], [DNS], 10)
    wait = retry(store, "linear")["next_at"] - time.time()
    assert 0 < wait <= cli.RETRY_NETWORK_MAX_SECONDS == 300


def test_other_failures_keep_the_long_backoff(store):
    fail(store, ["todoist"], [AUTH], 10)
    assert retry(store, "todoist")["next_at"] - time.time() > cli.RETRY_NETWORK_MAX_SECONDS


def test_network_back_makes_offline_sources_due_at_once(store, config):
    # The Mac was offline: every source failed several times.
    fail(store, ["linear", "matrix", "gmail"], [DNS, MATRIX, "gmail: URLError <urlopen error [Errno 8]>"], 6)
    assert set(cli.fast_sources(config, store)) & {"linear", "matrix", "gmail"} == set()
    # The network is back: Gmail reads fine on its nudge.
    cli.settle_checked(store, ["gmail"], [], time.time())
    due = cli.fast_sources(config, store)
    assert {"linear", "matrix"} <= set(due)


def test_recovery_retries_a_host_that_stays_down_only_once(store, config):
    # ZimaBlade alone is down; the other sources keep reading fine.
    fail(store, ["matrix"], [MATRIX], 3)
    cli.settle_checked(store, ["gmail"], [], time.time())
    assert "matrix" in cli.fast_sources(config, store)
    cli.settle_checked(store, ["matrix"], [MATRIX], time.time())
    # Further successes elsewhere do not keep it retrying on every check; its own backoff applies.
    cli.settle_checked(store, ["gmail"], [], time.time())
    assert "matrix" not in cli.fast_sources(config, store)


def test_recovery_does_not_skip_non_network_backoff(store, config):
    fail(store, ["todoist"], [AUTH], 3)
    cli.settle_checked(store, ["gmail"], [], time.time())
    assert "todoist" not in cli.fast_sources(config, store)


def test_a_success_clears_the_retry(store):
    fail(store, ["linear"], [DNS], 3)
    cli.settle_checked(store, ["linear"], [], time.time())
    assert retry(store, "linear") is None
