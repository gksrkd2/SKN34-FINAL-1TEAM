"""이미지 산식: 합산 후 한 번 올림하고 대기와 공종별 휴일을 구분한다."""

from datetime import date
from decimal import Decimal

import pytest

from app.calc import schedule as calc


def item(q="100", md="0.024", factor="1"):
    return calc.WorkItem("wall", Decimal(q), Decimal(md), Decimal(factor))


def test_formula_sums_parts_before_one_ceiling_and_ignores_helpers():
    assert hasattr(calc, "work_duration"), "이미지 산식 함수가 필요해요"
    assert calc.work_duration([item("1", "1.6")], 1) == 2
    assert calc.work_duration([item("1", "0.1"), item("1", "0.1")], 1) == 1
    assert calc.work_duration([item("100", "0.024"), item("20", "0.027", "1.3")], 2) == 2


@pytest.mark.parametrize("value", ["-1", "NaN", "Infinity"])
def test_nonfinite_or_negative_input_is_rejected(value):
    assert hasattr(calc, "work_duration"), "입력 검증 함수가 필요해요"
    with pytest.raises(ValueError):
        calc.work_duration([item(value)], 2)


@pytest.mark.parametrize("crew", [0, -1, True, 1.5])
def test_invalid_crew_is_rejected(crew):
    assert hasattr(calc, "work_duration"), "인원 검증 함수가 필요해요"
    with pytest.raises(ValueError):
        calc.work_duration([item()], crew)


def test_trade_calendars_wait_and_order_are_explicit():
    assert hasattr(calc, "expected_plan"), "공종별 달력이 필요해요"
    trades = [
        calc.ExpectedTrade("dobae", (item("1", "1"),), 1, wait_days=2),
        calc.ExpectedTrade("mokgong", (item("1", "1"),), 1),
    ]
    # 금요일 휴일에는 목공을 쉬고 토요일 작업 → 일요일 대기 → 월요일 도배 → 이틀 대기
    plan = calc.expected_plan(trades, date(2026, 12, 25), {date(2026, 12, 25)})
    work = [b for b in plan.blocks if b.kind == "work"]
    assert [(b.trade, b.start, b.end) for b in work] == [
        ("mokgong", date(2026, 12, 26), date(2026, 12, 26)),
        ("dobae", date(2026, 12, 28), date(2026, 12, 28)),
    ]
    assert plan.work_days == 2 and plan.end == date(2026, 12, 30)
    assert [d.kind for d in plan.days][-2:] == ["dry", "dry"]


def test_wallpaper_works_on_holiday_but_not_sunday():
    assert hasattr(calc, "expected_plan"), "공종별 달력이 필요해요"
    tp = calc.ExpectedTrade("dobae", (item("1", "2"),), 1)
    plan = calc.expected_plan([tp], date(2026, 12, 25), {date(2026, 12, 25)})
    assert [d.day for d in plan.days if d.kind == "work"] == [
        date(2026, 12, 25),
        date(2026, 12, 26),
    ]
    sunday = calc.expected_plan([tp], date(2026, 12, 27), {date(2026, 12, 27)})
    assert sunday.start == date(2026, 12, 28)


def test_empty_duplicate_unsupported_or_excessive_input_is_rejected():
    assert hasattr(calc, "expected_plan"), "공정 검증이 필요해요"
    tp = calc.ExpectedTrade("dobae", (item(),), 2)
    for trades in ([], [tp, tp], [calc.ExpectedTrade("bath", (item(),), 1)]):
        with pytest.raises(ValueError):
            calc.expected_plan(trades, date(2026, 1, 1))
    with pytest.raises(ValueError):
        calc.expected_plan(
            [calc.ExpectedTrade("dobae", (item(),), 2, wait_days=2000)], date(2026, 1, 1)
        )


def test_saturday_holidays_are_present_in_calendar_data():
    from app.refprice.holidays import HOLIDAYS

    for d in (
        date(2026, 6, 6),
        date(2026, 8, 15),
        date(2026, 10, 3),
        date(2027, 2, 6),
        date(2027, 5, 1),
        date(2027, 7, 17),
        date(2027, 10, 9),
        date(2027, 12, 25),
    ):
        assert d in HOLIDAYS
    assert date(2027, 5, 3) in HOLIDAYS and date(2027, 7, 19) in HOLIDAYS
