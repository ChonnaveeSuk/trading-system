# trading-system/strategy/src/filters/economic_calendar.py
#
# Hardcoded high-impact US-macro event calendar.
#
# Why hardcoded: FRED returns release dates, but mapping a release name to
# "high impact for the trading book" is editorial.  A static list keeps the
# filter behaviour deterministic, reviewable in PR diffs, and immune to
# upstream API outages on event days (the worst possible time to lose the
# filter).
#
# Coverage:
#   - 2026: full hand-curated FOMC + chair-speech calendar.
#   - 2027+: FOMC dates must be appended manually (Fed publishes in advance).
#   - NFP (first Friday of each month) is auto-generated for any year.
#   - CPI (~14th of each month, BLS publishes the calendar in November)
#     is auto-generated for any year as a sensible default; override the
#     2026 dates with the published BLS schedule.
#
# Blackout window:
#   - Event day itself
#   - + N preceding days (default 1) to avoid entering positions into
#     known volatility — a pre-event entry can be stopped out by the
#     event-day move before the thesis has any chance to play out.
#
# Returns SELL signals untouched in all cases — exits are never blocked.

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import Enum
from typing import Optional


class EventKind(str, Enum):
    """High-impact event categories."""
    FOMC = "FOMC"           # Fed rate decision (2-day meeting; we mark day 2)
    CPI = "CPI"             # Consumer Price Index release
    NFP = "NFP"             # Non-Farm Payrolls (first Friday)
    GDP = "GDP"             # Quarterly GDP advance estimate
    FED_SPEECH = "FED_SPEECH"  # Major Fed Chair remarks (Jackson Hole, semi-annual testimony)


@dataclass(frozen=True)
class EconomicEvent:
    """A single calendar event."""
    event_date: date
    kind: EventKind
    description: str

    @property
    def label(self) -> str:
        return f"{self.kind.value}: {self.description}"


# ── Hand-curated 2026 calendar ───────────────────────────────────────────────
# FOMC: 8 meetings/year, 2-day format; we mark the second day (rate decision).
# Source: https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
_FOMC_2026: list[EconomicEvent] = [
    EconomicEvent(date(2026, 1, 29), EventKind.FOMC, "FOMC Jan 28-29"),
    EconomicEvent(date(2026, 3, 19), EventKind.FOMC, "FOMC Mar 18-19"),
    EconomicEvent(date(2026, 5, 7),  EventKind.FOMC, "FOMC May 6-7"),
    EconomicEvent(date(2026, 6, 18), EventKind.FOMC, "FOMC Jun 17-18"),
    EconomicEvent(date(2026, 7, 30), EventKind.FOMC, "FOMC Jul 29-30"),
    EconomicEvent(date(2026, 9, 17), EventKind.FOMC, "FOMC Sep 16-17"),
    EconomicEvent(date(2026, 11, 5), EventKind.FOMC, "FOMC Nov 4-5"),
    EconomicEvent(date(2026, 12, 17), EventKind.FOMC, "FOMC Dec 16-17"),
]

# Fed Chair major speeches — Jackson Hole + semi-annual congressional testimony
_FED_SPEECH_2026: list[EconomicEvent] = [
    EconomicEvent(date(2026, 2, 11), EventKind.FED_SPEECH, "Semi-annual Senate testimony"),
    EconomicEvent(date(2026, 2, 12), EventKind.FED_SPEECH, "Semi-annual House testimony"),
    EconomicEvent(date(2026, 7, 15), EventKind.FED_SPEECH, "Semi-annual Senate testimony"),
    EconomicEvent(date(2026, 7, 16), EventKind.FED_SPEECH, "Semi-annual House testimony"),
    EconomicEvent(date(2026, 8, 21), EventKind.FED_SPEECH, "Jackson Hole keynote"),
]

# GDP advance estimate — quarterly, released ~end of first month after quarter close
_GDP_2026: list[EconomicEvent] = [
    EconomicEvent(date(2026, 1, 29), EventKind.GDP, "Q4-2025 advance"),
    EconomicEvent(date(2026, 4, 30), EventKind.GDP, "Q1-2026 advance"),
    EconomicEvent(date(2026, 7, 30), EventKind.GDP, "Q2-2026 advance"),
    EconomicEvent(date(2026, 10, 29), EventKind.GDP, "Q3-2026 advance"),
]


# ── Auto-generated event helpers ─────────────────────────────────────────────

def _first_friday(year: int, month: int) -> date:
    """First Friday of (year, month) — NFP release day."""
    d = date(year, month, 1)
    # Monday=0 … Friday=4
    days_ahead = (4 - d.weekday()) % 7
    return d + timedelta(days=days_ahead)


def _generate_nfp(year: int) -> list[EconomicEvent]:
    """First Friday of each month."""
    return [
        EconomicEvent(_first_friday(year, m), EventKind.NFP, f"Non-Farm Payrolls {year}-{m:02d}")
        for m in range(1, 13)
    ]


def _generate_cpi(year: int) -> list[EconomicEvent]:
    """Approx ~14th of each month (BLS releases mid-month).  Override with
    the published BLS calendar for year-specific accuracy."""
    return [
        EconomicEvent(date(year, m, 14), EventKind.CPI, f"CPI {year}-{m:02d}")
        for m in range(1, 13)
    ]


# ── EconomicCalendar ─────────────────────────────────────────────────────────

class EconomicCalendar:
    """Look up high-impact macro events around a given date.

    Usage::

        cal = EconomicCalendar()
        cal.is_blackout_day(date(2026, 5, 6))   # → True (FOMC tomorrow)
        cal.get_next_event(date(2026, 4, 28))   # → (event, days_away)
        cal.events_in_window(date(2026, 5, 1), date(2026, 5, 31))

    The `blackout_days` constructor arg sets how many calendar days BEFORE
    each event count as blacked out (default 1).  The event day itself is
    always blacked out.  E.g. for FOMC May 6-7, with blackout_days=1:
      May 6: BLACKOUT (day-of)
      May 7: BLACKOUT (day-of)  — N/A: only May 7 is in our list (decision day)
      May 6: BLACKOUT (day before May 7)
    """

    def __init__(
        self,
        blackout_days_before: int = 1,
        extra_events: Optional[list[EconomicEvent]] = None,
    ) -> None:
        self.blackout_days_before = blackout_days_before
        events: list[EconomicEvent] = list(
            _FOMC_2026 + _FED_SPEECH_2026 + _GDP_2026
            + _generate_nfp(2026) + _generate_cpi(2026)
        )
        if extra_events:
            events.extend(extra_events)
        # Keep events sorted for deterministic next-event lookups.
        events.sort(key=lambda e: e.event_date)
        self._events: list[EconomicEvent] = events

    # ── Coverage extension ────────────────────────────────────────────────

    def extend_year(self, year: int, fomc_dates: Optional[list[date]] = None) -> None:
        """Add NFP+CPI for `year`; optionally extend FOMC dates manually.

        FOMC dates are not auto-generated because the Fed publishes its
        meeting calendar a year in advance and the schedule is irregular.
        """
        new_events: list[EconomicEvent] = _generate_nfp(year) + _generate_cpi(year)
        if fomc_dates:
            for d in fomc_dates:
                new_events.append(EconomicEvent(d, EventKind.FOMC, f"FOMC {d.isoformat()}"))
        self._events.extend(new_events)
        self._events.sort(key=lambda e: e.event_date)

    # ── Lookups ───────────────────────────────────────────────────────────

    def is_blackout_day(self, d: date) -> bool:
        """True if `d` is an event day or within the pre-event blackout window."""
        return self.event_on(d) is not None or self.event_within(d, self.blackout_days_before) is not None

    def event_on(self, d: date) -> Optional[EconomicEvent]:
        """Return the event on `d`, or None.  Returns the first match if multiple."""
        for ev in self._events:
            if ev.event_date == d:
                return ev
        return None

    def event_within(self, d: date, days_ahead: int) -> Optional[EconomicEvent]:
        """Return the next event in (d, d + days_ahead], or None.

        `d` itself is excluded — use event_on() for the day-of check.
        """
        end = d + timedelta(days=days_ahead)
        for ev in self._events:
            if d < ev.event_date <= end:
                return ev
            if ev.event_date > end:
                break
        return None

    def get_next_event(self, d: date) -> Optional[tuple[EconomicEvent, int]]:
        """Return (next_event, days_away) where days_away ≥ 0; None if past calendar end."""
        for ev in self._events:
            if ev.event_date >= d:
                return ev, (ev.event_date - d).days
        return None

    def events_in_window(self, start: date, end: date) -> list[EconomicEvent]:
        """All events with start ≤ event_date ≤ end."""
        return [ev for ev in self._events if start <= ev.event_date <= end]

    def blackout_reason(self, d: date) -> Optional[str]:
        """Human-readable reason this date is blacked out, or None."""
        ev = self.event_on(d)
        if ev is not None:
            return f"{ev.kind.value} today: {ev.description}"
        ev = self.event_within(d, self.blackout_days_before)
        if ev is not None:
            days = (ev.event_date - d).days
            when = "tomorrow" if days == 1 else f"in {days} day(s)"
            return f"{ev.kind.value} {when}: {ev.description}"
        return None

    # ── Telemetry helpers ─────────────────────────────────────────────────

    @property
    def all_events(self) -> list[EconomicEvent]:
        """Read-only view of the loaded calendar (already sorted)."""
        return list(self._events)
