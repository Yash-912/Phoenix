"""The bridge's configuration: what it needs to start, and every way it refuses to.

An invalid value stops the process. A bridge that falls back to something looser
(no approver map, a channel it guessed, a token of the wrong kind) would look
healthy while posting buttons nobody could use.
"""

import dataclasses

import pytest

from phoenix.slack.config import SlackConfigError, load_config

VALID = {
    "SLACK_APP_TOKEN": "xapp-1-A0123456789-1234567890-abcdef",
    "SLACK_BOT_TOKEN": "xoxb-1234567890-abcdefghijkl",
    "PHOENIX_SLACK_CHANNEL": "C0123456789",
    "PHOENIX_SLACK_APPROVERS": "U012ABCDEF=alice,U034DEFGHI=bob",
    "PHOENIX_APPROVAL_TOKEN": "approval-token-for-tests",
    "PHOENIX_DASHBOARD_URL": "http://localhost:5000",
    "DATABASE_URL": "postgresql://slack_bridge:test@postgres:5432/phoenix",
}


def _env(**changes):
    env = dict(VALID)
    for name, value in changes.items():
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value
    return env


def test_a_valid_environment_gives_a_config_with_the_defaults():
    config = load_config(VALID)

    assert config.channel_id == "C0123456789"
    assert config.approvers == {"U012ABCDEF": "alice", "U034DEFGHI": "bob"}
    assert config.api_url == "http://phoenix-api:8000"
    assert config.poll_seconds == 5
    assert config.dashboard_url == "http://localhost:5000"


def test_the_config_cannot_be_changed_after_it_is_loaded():
    config = load_config(VALID)

    with pytest.raises(dataclasses.FrozenInstanceError):
        config.channel_id = "C9999999999"
    with pytest.raises(TypeError):
        config.approvers["U999AAAAAA"] = "mallory"


def test_urls_lose_a_trailing_slash_so_links_are_built_the_same_way_every_time():
    config = load_config(_env(PHOENIX_DASHBOARD_URL="https://phoenix.example.com/", PHOENIX_API_URL="http://api:8000/"))

    assert config.dashboard_url == "https://phoenix.example.com"
    assert config.api_url == "http://api:8000"


@pytest.mark.parametrize("name", [
    "SLACK_APP_TOKEN",
    "SLACK_BOT_TOKEN",
    "PHOENIX_SLACK_CHANNEL",
    "PHOENIX_SLACK_APPROVERS",
    "PHOENIX_APPROVAL_TOKEN",
    "PHOENIX_DASHBOARD_URL",
    "DATABASE_URL",
])
def test_a_missing_required_setting_names_the_variable(name):
    with pytest.raises(SlackConfigError, match=name):
        load_config(_env(**{name: None}))
    with pytest.raises(SlackConfigError, match=name):
        load_config(_env(**{name: "   "}))


@pytest.mark.parametrize("name, value", [
    ("SLACK_APP_TOKEN", "xoxb-1234567890-abcdefghijkl"),
    ("SLACK_APP_TOKEN", "not-a-token"),
    ("SLACK_BOT_TOKEN", "xapp-1-A0123456789-1234567890-abcdef"),
    ("SLACK_BOT_TOKEN", "not-a-token"),
])
def test_a_token_of_the_wrong_kind_is_refused_by_its_prefix(name, value):
    with pytest.raises(SlackConfigError, match=name):
        load_config(_env(**{name: value}))


def test_the_error_never_contains_the_token_it_rejected():
    with pytest.raises(SlackConfigError) as caught:
        load_config(_env(SLACK_APP_TOKEN="xoxb-this-secret-is-in-the-wrong-variable"))

    assert "this-secret-is-in-the-wrong-variable" not in str(caught.value)


@pytest.mark.parametrize("channel", ["general", "#general", "c0123456789", "C12", "C0123 456789"])
def test_a_channel_must_be_an_id_not_a_name(channel):
    with pytest.raises(SlackConfigError, match="PHOENIX_SLACK_CHANNEL"):
        load_config(_env(PHOENIX_SLACK_CHANNEL=channel))


@pytest.mark.parametrize("approvers", [
    "alice",
    "U012ABCDEF=",
    "=alice",
    "U012ABCDEF=alice,bob",
    "alice=U012ABCDEF",
    "U012ABCDEF=alice,U012ABCDEF=bob",
    ",",
])
def test_a_malformed_approver_map_is_refused(approvers):
    with pytest.raises(SlackConfigError, match="PHOENIX_SLACK_APPROVERS"):
        load_config(_env(PHOENIX_SLACK_APPROVERS=approvers))


def test_spaces_and_a_trailing_comma_in_the_approver_map_are_tolerated():
    config = load_config(_env(PHOENIX_SLACK_APPROVERS=" U012ABCDEF = alice , U034DEFGHI=bob ,"))

    assert config.approvers == {"U012ABCDEF": "alice", "U034DEFGHI": "bob"}


@pytest.mark.parametrize("name, value", [
    ("PHOENIX_DASHBOARD_URL", "localhost:5000"),
    ("PHOENIX_DASHBOARD_URL", "ftp://localhost"),
    ("PHOENIX_API_URL", "phoenix-api:8000"),
])
def test_a_url_without_a_scheme_is_refused(name, value):
    with pytest.raises(SlackConfigError, match=name):
        load_config(_env(**{name: value}))


@pytest.mark.parametrize("value", ["fast", "0", "-5", "nan", "inf"])
def test_the_poll_interval_must_be_a_positive_number(value):
    with pytest.raises(SlackConfigError, match="PHOENIX_SLACK_POLL_SECONDS"):
        load_config(_env(PHOENIX_SLACK_POLL_SECONDS=value))


def test_the_poll_interval_can_be_set():
    assert load_config(_env(PHOENIX_SLACK_POLL_SECONDS="2.5")).poll_seconds == 2.5
