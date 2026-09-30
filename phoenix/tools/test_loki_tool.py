import pytest

from phoenix.tools import loki_tool


@pytest.mark.parametrize(
    "bad",
    ["15", 0, -5, 100000, 15.5, True, None],
    ids=["str", "zero", "negative", "too-large", "float", "bool", "none"],
)
def test_a_minutes_value_that_is_not_a_sane_int_is_refused_without_allocating(bad):
    result = loki_tool.query_loki('{container="checkout-service"}', minutes=bad)

    assert result == {"status": "error", "error": f"invalid minutes: {bad!r}"}
