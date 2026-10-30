"""The dashboard-side redactor: planted secrets must not survive, counters must.

This lowers the risk of a credential reaching a screen; it is not the graph's own
redaction and these tests do not claim it is.
"""

from redact import Redactor

JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnopqrstuvwx"


def run(value):
    redactor = Redactor()
    return redactor.value(value), redactor


def test_planted_secrets_do_not_survive_in_text():
    planted = [
        JWT,
        "AKIAIOSFODNN7EXAMPLE",
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "xoxb-1234567890-abcdefghij",
        "sk-abcdefghijklmnopqrstuvwxyz",
        "postgresql://phoenix_app:hunter2hunter2@postgres:5432/phoenix",
        "Authorization: Bearer abcDEF123456.ghi",
        "password=correcthorse",
        "api_key: 0123456789abcdef",
        "alice@example.com",
        "4111 1111 1111 1111",
    ]
    out, redactor = run(" | ".join(planted))
    for needle in (
        JWT, "AKIAIOSFODNN7EXAMPLE", "ghp_abcdefghijklmnopqrstuvwxyz0123456789", "xoxb-1234567890-abcdefghij",
        "sk-abcdefghijklmnopqrstuvwxyz", "hunter2hunter2", "abcDEF123456.ghi", "correcthorse", "0123456789abcdef",
        "alice@example.com", "4111 1111 1111 1111",
    ):
        assert needle not in out
    assert redactor.summary()["card"] == 1


def test_connection_string_keeps_its_shape_and_loses_its_password():
    out, _ = run("postgresql://phoenix_app:hunter2hunter2@postgres:5432/phoenix")
    assert out.startswith("postgresql://phoenix_app:[CONNECTION_STRING_1]@postgres:5432")


def test_same_value_gets_the_same_label_and_a_different_value_a_new_one():
    out, redactor = run("a@example.com wrote to b@example.com and a@example.com again")
    assert out == "[EMAIL_1] wrote to [EMAIL_2] and [EMAIL_1] again"
    assert redactor.summary() == {"email": 3}


def test_secret_named_fields_are_blanked_whatever_the_value_looks_like():
    out, redactor = run({"password": "x", "api_token": "y", "Authorization": "z", "nested": [{"cookie": "c"}]})
    assert out == {"password": "[REDACTED]", "api_token": "[REDACTED]", "Authorization": "[REDACTED]", "nested": [{"cookie": "[REDACTED]"}]}
    assert redactor.summary()["secret_field"] == 4


def test_counters_named_like_secrets_are_left_alone():
    value = {"tokens_spent": 1200, "token_budget": 50000, "approver": "tester", "decided_by": "tester"}
    out, redactor = run(value)
    assert out == value
    assert redactor.summary() == {}


def test_empty_secret_field_is_not_counted():
    out, redactor = run({"token": "", "password": None})
    assert out == {"token": "", "password": None}
    assert redactor.summary() == {}


def test_a_number_that_fails_the_luhn_check_is_not_a_card():
    out, redactor = run("order 4111 1111 1111 1112 shipped, port 5432")
    assert "4111 1111 1111 1112" in out
    assert "card" not in redactor.summary()


def test_input_is_not_mutated():
    original = {"password": "x", "list": ["alice@example.com"]}
    run(original)
    assert original == {"password": "x", "list": ["alice@example.com"]}


def test_container_names_versions_and_timestamps_pass_through():
    value = {"container": "checkout-service", "version": "v18", "at": "2026-10-30T14:51:08.111759+00:00", "url": "http://worker-service:8000/health"}
    out, redactor = run(value)
    assert out == value
    assert redactor.summary() == {}
