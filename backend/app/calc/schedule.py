"""공정표 날짜 계산: 공정을 하루 단위로 잇고, 공사 날짜 · 기한 · 연락처 기한을 센다.

- 공정 기간: 단계마다 반나절 단위로 센 것을 더해 하루 단위로 올린다(공정 계산이 단계를 만든다).
- 잇기: 넘긴 순서대로 공정을 놓고, 뒤 공정은 앞 공정이 끝난 다음 일하는 날 시작한다.
  날짜를 정한 공정(업체가 냄 · 고객이 수락함)은 그 날짜에 두고 단계를 그 안의 일하는 날에 나눈다.
  일하는 날은 주말 요일과 쉬는 날(공휴일)을 뺀 날이다. 짐 정리 단계는 처음 한 번만 둔다.
- 도배가 있으면 끝난 다음 날부터 자연 건조(달력 날짜), 그다음 입주 청소(고객이 직접)를 둔다.
- 기한: 기준 시각의 서울 날짜 + N일의 18:00(달력 날짜).
- 연락처 기한: 업체가 마지막으로 '공사 끝냈어요'를 올린 날의 1년 뒤 같은 날(2월 29일이면 2월 28일).
"""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_CEILING, Decimal
from typing import Literal

from app.calc.common import Step
from app.shared.clock import SEOUL

WEEKEND = (5, 6)  # 토 · 일 (date.weekday 값)
MAX_SPAN_DAYS = 3 * 366  # 이만큼 넘겨도 일할 날이 모자라면 멈춘다
JOB_MAX_DAYS = 120  # 업체가 낼 수 있는 한 공정의 공사 날짜 범위
CONTACT_YEARS = 1  # 전문공사 하자담보책임기간(1~3년) 중 가장 짧은 것
DRY_DAYS = 2  # 도배 뒤 자연 건조 (도배 시방서: 창문을 닫고 천천히 말린다)
CLEAN_DAYS = 1  # 입주 청소 (고객이 직접)
DUE_HOUR = 18

BlockKind = Literal["work", "dry", "clean"]


@dataclass(frozen=True)
class TradePlan:
    """공정 하나: 공정 계산이 낸 단계와 하루 인원."""

    trade: str
    steps: Sequence[Step]
    crew: int


@dataclass(frozen=True)
class Block:
    trade: str | None  # 건조 · 청소는 공정이 없다
    kind: BlockKind
    label: str
    start: date
    end: date
    days: int  # 일하는 날(작업) 또는 달력 날짜(건조 · 청소)
    crew: int | None


@dataclass(frozen=True)
class DayTask:
    day: date
    trade: str | None
    kind: BlockKind | Literal["off"]
    names: tuple[str, ...]


@dataclass(frozen=True)
class Plan:
    blocks: list[Block]
    days: list[DayTask]  # 시작일부터 마지막 날까지 하루씩(쉬는 날 포함)
    work_days: int  # 작업하는 날 수 (건조 · 청소 빼고)
    start: date
    end: date


EXPECTED_FORMULA_VERSION = "image-2026.11.12"


@dataclass(frozen=True)
class WorkItem:
    """같은 주 직종의 부위별 물량과 품. 보조 인력 품은 전달하지 않는다."""

    part: str
    quantity: Decimal
    man_day: Decimal
    factor: Decimal


@dataclass(frozen=True)
class ExpectedTrade:
    trade: str
    items: Sequence[WorkItem]
    crew: int
    wait_days: int = 0  # 이 공정 뒤 작업하지 않고 기다리는 달력 날짜


def work_duration(items: Sequence[WorkItem], crew: int) -> int:
    """이미지 산식: ceil(Σ(물량 × 주 직종 품 × 할증) / 주 직종 인원)."""
    if type(crew) is not int or crew < 1 or not items:
        raise ValueError("물량과 주 직종 인원을 확인해 주세요")
    for row in items:
        if (
            any(
                not isinstance(v, Decimal) or not v.is_finite() or v < 0
                for v in (row.quantity, row.man_day, row.factor)
            )
            or row.factor == 0
        ):
            raise ValueError("물량·품·할증은 유한한 수이고 할증은 양수여야 해요")
    total = sum((r.quantity * r.man_day * r.factor for r in items), Decimal(0))
    if total <= 0:
        raise ValueError("주 직종 작업량이 있어야 해요")
    days = int((total / crew).to_integral_value(rounding=ROUND_CEILING))
    if days > JOB_MAX_DAYS:
        raise ValueError("작업 기간이 계산 한도를 넘었어요")
    return days


def expected_plan(
    trades: Sequence[ExpectedTrade], start: date, holidays: Collection[date] = ()
) -> Plan:
    """사용자 물량·인원과 검증한 품만으로 예상 일정을 만든다. 준비·청소 기간을 더하지 않는다.

    목공 → 도배. 토요일 작업, 일요일 휴무, 공휴일은 목공만 휴무다.
    대기는 공휴일을 포함한 달력 날짜이며 시공일과 분리한다.
    """
    order = ("mokgong", "dobae")
    if not trades or len({t.trade for t in trades}) != len(trades):
        raise ValueError("공정을 중복 없이 골라 주세요")
    if any(
        t.trade not in order or type(t.wait_days) is not int or not 0 <= t.wait_days <= JOB_MAX_DAYS
        for t in trades
    ):
        raise ValueError("지원 공정과 대기일을 확인해 주세요")
    blocks, tasks = [], {}
    cursor = start
    for trade in sorted(trades, key=lambda t: order.index(t.trade)):
        count = work_duration(trade.items, trade.crew)
        dates = workdays(cursor, count, holidays if trade.trade == "mokgong" else (), (6,))
        if (dates[-1] - start).days + trade.wait_days > MAX_SPAN_DAYS:
            raise ValueError("전체 일정이 계산 한도를 넘었어요")
        blocks.append(
            Block(
                trade.trade,
                "work",
                "목공" if trade.trade == "mokgong" else "도배",
                dates[0],
                dates[-1],
                count,
                trade.crew,
            )
        )
        names = tuple(dict.fromkeys(r.part for r in trade.items))
        for day in dates:
            tasks[day] = DayTask(day, trade.trade, "work", names)
        cursor = dates[-1] + timedelta(days=1)
        if trade.wait_days:
            end = cursor + timedelta(days=trade.wait_days - 1)
            blocks.append(Block(None, "dry", "비시공 대기", cursor, end, trade.wait_days, None))
            for i in range(trade.wait_days):
                day = cursor + timedelta(days=i)
                tasks[day] = DayTask(day, None, "dry", ("비시공 대기",))
            cursor = end + timedelta(days=1)
    first, end = blocks[0].start, cursor - timedelta(days=1)
    days = [
        tasks.get(day, DayTask(day, None, "off", ()))
        for i in range((end - first).days + 1)
        if (day := first + timedelta(days=i))
    ]
    return Plan(blocks, days, sum(b.days for b in blocks if b.kind == "work"), first, end)


def is_off(d: date, days_off: Collection[date], weekend: Collection[int]) -> bool:
    return d.weekday() in weekend or d in days_off


def workdays(
    start: date,
    count: int,
    days_off: Collection[date] = (),
    weekend: Collection[int] = WEEKEND,
) -> list[date]:
    """start부터 일하는 날 count개. start가 쉬는 날이면 다음 일하는 날부터 센다."""
    if count < 0 or not set(weekend) <= set(range(7)) or len(set(weekend)) == 7:
        raise ValueError("일하는 날 수와 주말 요일을 다시 확인해 주세요")
    out: list[date] = []
    d, last = start, start + timedelta(days=MAX_SPAN_DAYS)
    while len(out) < count:
        if d > last:
            raise ValueError("쉬는 날이 너무 많아 일할 날을 찾지 못했어요")
        if not is_off(d, days_off, weekend):
            out.append(d)
        d += timedelta(days=1)
    return out


def trade_days(steps: Sequence[Step]) -> int:
    """단계 반나절을 더해 하루 단위로 올린다."""
    halves = sum(s.half_days for s in steps)
    if halves < 1 or any(s.half_days < 1 for s in steps):
        raise ValueError("단계는 반나절 이상이어야 해요")
    return (halves + 1) // 2


def lay_out(
    trades: Sequence[TradePlan],
    start: date,
    days_off: Collection[date] = (),
    weekend: Collection[int] = WEEKEND,
) -> Plan:
    """공정을 순서대로 하루 단위로 잇는다. trades가 비면 ValueError."""
    return fix_plan(trades, {}, start, days_off, weekend)


def _range_days(
    d_from: date, d_to: date, days_off: Collection[date], weekend: Collection[int]
) -> list[date]:
    if d_to < d_from or (d_to - d_from).days > JOB_MAX_DAYS:
        raise ValueError("공사 날짜 범위를 다시 확인해 주세요")
    span = (d_from + timedelta(days=i) for i in range((d_to - d_from).days + 1))
    days = [d for d in span if not is_off(d, days_off, weekend)]
    if not days:
        raise ValueError("정한 날짜 안에 일하는 날이 없어요")
    return days


def fix_plan(
    trades: Sequence[TradePlan],
    fixed: Mapping[str, tuple[date, date]],
    start: date,
    days_off: Collection[date] = (),
    weekend: Collection[int] = WEEKEND,
) -> Plan:
    """공정을 순서대로 놓는다. fixed에 있는 공정은 그 날짜(업체가 낸 · 고객이 수락한 날짜)에 두고,
    없는 공정은 앞 공정이 끝난 다음 일하는 날(첫 공정이면 start)부터 계산한 기간만큼 놓는다.
    단계는 그 공정의 일하는 날에 반나절 비율로 나눠 놓는다. 건조 · 청소는 도배 · 마지막 작업 뒤."""
    if not trades:
        raise ValueError("공정이 하나 이상 있어야 해요")
    seen_prep = False
    blocks: list[Block] = []
    tasks: dict[date, tuple[str | None, BlockKind, list[str]]] = {}
    cursor = start
    for tp in trades:
        steps = [s for s in tp.steps if not (s.key == "prep" and seen_prep)]
        seen_prep = seen_prep or any(s.key == "prep" for s in steps)
        if tp.trade in fixed:
            days = _range_days(*fixed[tp.trade], days_off, weekend)
        else:
            days = workdays(cursor, trade_days(steps), days_off, weekend)
        halves = [s for s in steps for _ in range(s.half_days)]
        n, total = len(days), len(halves)
        for k, d in enumerate(days):
            got = [halves[h] for h in range(total) if h * n // total == k]
            for s in got or [halves[k * total // n]]:  # 날이 반나절보다 많으면 그때 하던 단계
                entry = tasks.setdefault(d, (tp.trade, "work", []))
                if entry[0] == tp.trade and s.name not in entry[2]:
                    entry[2].append(s.name)
        blocks.append(Block(tp.trade, "work", "", days[0], days[-1], n, tp.crew))
        cursor = days[-1] + timedelta(days=1)
    work_end = max(b.end for b in blocks)
    dobae = next((b for b in blocks if b.trade == "dobae"), None)
    if dobae is not None:
        dry_start = dobae.end + timedelta(days=1)
        dry_end = dry_start + timedelta(days=DRY_DAYS - 1)
        blocks.append(Block(None, "dry", "자연 건조", dry_start, dry_end, DRY_DAYS, None))
        for i in range(DRY_DAYS):
            tasks[dry_start + timedelta(days=i)] = (None, "dry", ["창문을 닫고 자연 건조"])
        work_end = max(work_end, dry_end)
    clean_start = work_end + timedelta(days=1)
    clean_end = clean_start + timedelta(days=CLEAN_DAYS - 1)
    blocks.append(Block(None, "clean", "입주 청소", clean_start, clean_end, CLEAN_DAYS, None))
    for i in range(CLEAN_DAYS):
        tasks[clean_start + timedelta(days=i)] = (None, "clean", ["입주 청소 · 고객이 직접"])

    first = min(b.start for b in blocks)
    out: list[DayTask] = []
    d = first
    while d <= clean_end:
        if d in tasks:
            trade, kind, names = tasks[d]
            out.append(DayTask(d, trade, kind, tuple(names)))
        else:
            out.append(DayTask(d, None, "off", ()))
        d += timedelta(days=1)
    work = sum(b.days for b in blocks if b.kind == "work")
    return Plan(blocks, out, work, first, clean_end)


def count_workdays(
    date_from: date,
    date_to: date,
    days_off: Collection[date] = (),
    weekend: Collection[int] = WEEKEND,
) -> int:
    """시작일부터 끝날까지(둘 다 넣어서) 일하는 날 수."""
    span = (date_to - date_from).days
    if span < 0 or span > JOB_MAX_DAYS:
        raise ValueError("공사 날짜 범위를 다시 확인해 주세요")
    days = (date_from + timedelta(days=i) for i in range(span + 1))
    return sum(1 for d in days if not is_off(d, days_off, weekend))


@dataclass(frozen=True)
class Dated:
    trade: str
    date_from: date
    date_to: date


def order_conflicts(dated: Sequence[Dated], order: Sequence[str]) -> list[tuple[str, str]]:
    """순서(앞 → 뒤)가 정해진 공정에서 뒤 공정이 앞 공정이 끝나기 전에 시작하면 (앞, 뒤)."""
    if any(d.date_to < d.date_from for d in dated):
        raise ValueError("끝날이 시작일보다 앞이에요")
    by = {d.trade: d for d in dated}
    ranked = [t for t in order if t in by]
    out = []
    for i, a in enumerate(ranked):
        for b in ranked[i + 1 :]:
            if by[b].date_from <= by[a].date_to:
                out.append((a, b))
    return out


def _prev_workday(d: date, days_off: Collection[date], weekend: Collection[int]) -> date:
    for _ in range(MAX_SPAN_DAYS):
        if not is_off(d, days_off, weekend):
            return d
        d -= timedelta(days=1)
    raise ValueError("쉬는 날이 너무 많아 일할 날을 찾지 못했어요")


def free_range(
    trade: str,
    dated: Sequence[Dated],
    order: Sequence[str],
    days_off: Collection[date] = (),
    weekend: Collection[int] = WEEKEND,
) -> tuple[date | None, date | None]:
    """trade가 다른 공정과 겹치지 않는 자리: (앞 공정이 끝난 다음 일하는 날, 뒤 공정 시작 전 마지막
    일하는 날). 앞 · 뒤 공정이 없으면 그쪽은 None."""
    rank = {t: i for i, t in enumerate(order)}
    me = rank[trade]
    others = [d for d in dated if d.trade != trade and d.trade in rank]
    before = [d.date_to for d in others if rank[d.trade] < me]
    after = [d.date_from for d in others if rank[d.trade] > me]
    start_from = (
        workdays(max(before) + timedelta(days=1), 1, days_off, weekend)[0] if before else None
    )
    end_by = _prev_workday(min(after) - timedelta(days=1), days_off, weekend) if after else None
    return start_from, end_by


def start_choices(
    first: date,
    days: int,
    n: int,
    skip: Collection[date] = (),
    days_off: Collection[date] = (),
    weekend: Collection[int] = WEEKEND,
    end_by: date | None = None,
) -> list[tuple[date, date]]:
    """first부터 일하는 날 n개를 시작일 후보로 (skip은 빼고), 각각 일하는 날 days개면 끝나는 날.
    end_by가 있으면 그날까지 끝나는 후보만 (뒤 공정과 겹치지 않게). 모자라면 n개보다 적다."""
    if days < 1 or n < 0:
        raise ValueError("일하는 날 수와 후보 수를 다시 확인해 주세요")
    out: list[tuple[date, date]] = []
    d = first
    while len(out) < n:
        (start,) = workdays(d, 1, days_off, weekend)
        end = workdays(start, days, days_off, weekend)[-1]
        if end_by is not None and end > end_by:
            break  # 뒤로 갈수록 끝나는 날도 늦어진다
        if start not in skip:
            out.append((start, end))
        d = start + timedelta(days=1)
    return out


def due_at(base: datetime, days: int, hour: int = DUE_HOUR) -> datetime:
    """기준 시각의 서울 날짜 + days일 hour시(서울)."""
    if base.tzinfo is None or days < 0:
        raise ValueError("기준 시각은 시간대가 있어야 하고 일수는 0 이상이어야 해요")
    day = base.astimezone(SEOUL).date() + timedelta(days=days)
    return datetime.combine(day, time(hour), SEOUL)


def contact_until(work_done: date) -> date:
    """연락처가 서로 보이는 마지막 날: 1년 뒤 같은 날 (2월 29일이면 2월 28일)."""
    year = work_done.year + CONTACT_YEARS
    try:
        return work_done.replace(year=year)
    except ValueError:
        return work_done.replace(year=year, day=28)


def years_since(start: date, today: date) -> int:
    """start부터 오늘까지 꽉 찬 햇수 (업력). 아직 안 온 날이면 0."""
    years = today.year - start.year - ((today.month, today.day) < (start.month, start.day))
    return max(years, 0)


def days_since(at: datetime, today: date) -> int:
    """그 시각의 서울 날짜부터 오늘까지 지난 날 수 (같은 날이면 0). 심사 기다린 날에 쓴다."""
    if at.tzinfo is None:
        raise ValueError("시각은 시간대가 있어야 해요")
    return max((today - at.astimezone(SEOUL).date()).days, 0)
