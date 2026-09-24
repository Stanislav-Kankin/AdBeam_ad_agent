from decimal import Decimal, InvalidOperation

import pytest

from app.bot.menu import parse_target


@pytest.mark.parametrize(
    ("text", "expected"),
    [("2500", Decimal(2500)), ("2 500", Decimal(2500)), ("2500,5", Decimal("2500.5"))],
)
def test_target_input_accepts_russian_number_formats(text, expected):
    assert parse_target(text) == expected


@pytest.mark.parametrize("text", ["0", "-", "нет"])
def test_target_input_can_clear_target(text):
    assert parse_target(text) is None


@pytest.mark.parametrize("text", ["-5", "abc", "inf", "NaN"])
def test_target_input_rejects_invalid_values(text):
    with pytest.raises(InvalidOperation):
        parse_target(text)
