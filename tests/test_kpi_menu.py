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


def test_exclusion_words_are_parsed():
    from app.bot.menu import parse_exclusions

    assert parse_exclusions(" бренд, brand ;бренд") == ["бренд", "brand"]
    assert parse_exclusions("нет") == []
    import pytest

    with pytest.raises(ValueError):
        parse_exclusions("б")


async def test_excluded_brand_campaigns_are_not_raised_as_risks(runtime, monkeypatch):
    # Dima: Grand Line brand campaigns are kept on top at any price and must not
    # fill "needs attention" just because they spend the most.
    from app.analytics.periods import make_period
    from app.domain.reports import CheckMode
    from app.reporting.formatter import card

    client = runtime.registry.clients["west_export"]
    period = make_period("7d")
    before = await runtime.checks.analyze(client, period, CheckMode.STANDARD)
    assert any("РСЯ" in row["name"] for row in before.drivers)
    updated = await runtime.checks.repository.save_client_preferences(
        client, targets={"excluded_campaigns": ["РСЯ"]}
    )
    report = await runtime.checks.analyze(updated, period, CheckMode.STANDARD)
    assert not any("РСЯ" in str(s.actual.get("name", "")) for s in report.signals)
    assert all("РСЯ" not in row["name"] for row in report.drivers)
    text = card(report)
    assert "Кампании со словами «РСЯ» в рисках не подсвечиваются." in text
    assert report.current.spend == before.current.spend  # totals keep their spend
