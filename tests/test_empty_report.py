from app.analytics.periods import make_period
from app.domain.reports import CheckMode, Metrics
from app.reporting.formatter import detailed


async def test_empty_report_explains_scope_without_zero_goal_dump(runtime, client):
    report = await runtime.checks.analyze(client, make_period(), CheckMode.STANDARD)
    report.mock = False
    report.current = Metrics()
    report.previous = Metrics()
    report.source_status["Директ"] = "no_data"
    report.goal_metrics = [
        dict(id=str(i), name=f"Цель {i}", reaches=0, previous_reaches=0) for i in range(95)
    ]
    text = detailed(report)
    assert "Директ ответил, но не вернул строк статистики" in text
    assert "По 95 — ноль достижений" in text
    assert "не все обращения на сайте" in text
    assert "Цель 94" not in text
    assert "сейчас не рассчитано" not in text
    assert "Если статистика есть" in text
    assert len(report.goal_metrics) == 95


async def test_unavailable_report_does_not_claim_no_activity(runtime, client):
    report = await runtime.checks.analyze(client, make_period(), CheckMode.STANDARD)
    report.current = Metrics()
    report.source_status["Директ"] = "unavailable"
    text = detailed(report)
    assert "Это не означает нулевой расход" in text
    assert "Директ ответил, но" not in text
