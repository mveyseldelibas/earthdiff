"""Parsing of time windows such as ``2023-06`` or ``2023-06-01/2023-07-15``."""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class Period:
    start: date
    end: date  # inclusive

    def stac(self) -> str:
        return f"{self.start.isoformat()}T00:00:00Z/{self.end.isoformat()}T23:59:59Z"

    def label(self) -> str:
        if self.start.day == 1 and self.end == _month_end(self.end.year, self.end.month):
            if (self.start.year, self.start.month) == (self.end.year, self.end.month):
                return f"{self.start.year}-{self.start.month:02d}"
            return f"{self.start.year}-{self.start.month:02d} .. {self.end.year}-{self.end.month:02d}"
        return f"{self.start.isoformat()} .. {self.end.isoformat()}"

    def midpoint(self) -> date:
        return self.start + (self.end - self.start) / 2

    def shift_years(self, years: int) -> "Period":
        return Period(_shift(self.start, years), _shift(self.end, years))

    def days(self) -> int:
        return (self.end - self.start).days + 1


def _shift(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:  # 29 Feb
        return d.replace(year=d.year + years, day=28)


def _month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


_MONTH = re.compile(r"^(\d{4})-(\d{2})$")
_DAY = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def _parse_edge(text: str, is_end: bool) -> date:
    text = text.strip()
    m = _MONTH.match(text)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        if not 1 <= mo <= 12:
            raise ValueError(f"invalid month in {text!r}")
        return _month_end(y, mo) if is_end else date(y, mo, 1)
    m = _DAY.match(text)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    raise ValueError(f"cannot parse {text!r}; use YYYY-MM, YYYY-MM-DD or START/END")


def parse_period(text: str) -> Period:
    """``2023-06`` | ``2023-06-10`` | ``2023-06/2023-07`` | ``2023-06-01/2023-07-15``."""
    if "/" in text or ".." in text:
        a, b = re.split(r"/|\.\.", text, maxsplit=1)
        start, end = _parse_edge(a, False), _parse_edge(b, True)
    else:
        start, end = _parse_edge(text, False), _parse_edge(text, True)
    if end < start:
        raise ValueError(f"period end is before start: {text!r}")
    return Period(start, end)


def previous_month(today: date) -> Period:
    first = today.replace(day=1)
    last_prev = first - timedelta(days=1)
    return Period(last_prev.replace(day=1), last_prev)


def season_gap_days(a: Period, b: Period) -> int:
    """Circular distance (days) between the day-of-year of the two window midpoints."""
    da = a.midpoint().timetuple().tm_yday
    db = b.midpoint().timetuple().tm_yday
    diff = abs(da - db) % 365
    return min(diff, 365 - diff)
