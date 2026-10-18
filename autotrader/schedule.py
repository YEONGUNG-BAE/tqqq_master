"""Exchange sessions in UTC; ZoneInfo handles DST instead of fixed Korean times."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .core import Stop

NY = ZoneInfo("America/New_York")
KST = ZoneInfo("Asia/Seoul")


class Schedule:
    def __init__(self, now):
        import exchange_calendars as xc
        # Work from the documented schedule table, independent of pandas' timestamp
        # resolution. Nasdaq and NYSE share regular-session holidays/opening hours.
        year = now.astimezone(NY).year
        calendar = xc.get_calendar("XNYS", start=f"{year-2}-01-01", end=f"{year+1}-12-31")
        self.sessions = [(index.date().isoformat(), row["open"].to_pydatetime(), row["close"].to_pydatetime())
                         for index, row in calendar.schedule.iterrows()]

    def expected_close(self, now):
        completed = [s for s in self.sessions if s[2] + timedelta(minutes=5) <= now]
        if not completed:
            raise Stop("Calendar has no completed session")
        return completed[-1][0]

    def recent_dates(self, last, count=252):
        dates = [s[0] for s in self.sessions if s[0] <= last]
        if len(dates) < count:
            raise Stop("Calendar history shorter than required signal window")
        return dates[-count:]

    def trading_session(self, now):
        return next((s for s in self.sessions if s[1] <= now < s[2]), None)

    def execution_window(self, now):
        session = self.trading_session(now)
        return bool(session and session[1] + timedelta(minutes=1) <= now < session[1] + timedelta(minutes=15))

    def session_key(self, now):
        """Journal session label: US session date, else the Korean FX day."""
        session = self.trading_session(now)
        return session[0] if session else "KRW:" + now.astimezone(KST).date().isoformat()

    def next_session(self, now):
        return next((s for s in self.sessions if s[1] > now), None)

    @staticmethod
    def exchange_window(now):
        # Conservative daytime window. Actual broker availability remains the final
        # check; Korean holidays are not inferred from US exchange calendars.
        local = now.astimezone(KST)
        minute = local.hour * 60 + local.minute
        return local.weekday() < 5 and 9 * 60 <= minute < 15 * 60 + 20


# ---- US regular-session confirmation: calendar + broker segment + clock ----

REGULAR_CANDIDATE_CODES = frozenset({"004", "011"})
REGULAR_SESSION_UNVERIFIED = (
    "Broker market-hours code and segment times not yet observed on the real account",
    "Whether code 011 during the regular session carries the regular open/close times",
    "Whether the broker segment end reflects early closes (half days)",
    "Whether broker segment times follow the US daylight-saving switch on the same day",
)


def _hhmm(value):
    text = str(value)
    if len(text) != 4 or not text.isdigit() or int(text[:2]) > 23 or int(text[2:]) > 59:
        raise ValueError("invalid HHMM")
    return int(text[:2]), int(text[2:])


def broker_segment(begin, end, now):
    """Absolute [start, end) for a broker KST HHMM segment containing `now`.

    The segment may cross midnight (end <= begin), so it can have started on the
    previous Korean date. Returns None when `now` lies outside the segment."""
    (bh, bm), (eh, em) = _hhmm(begin), _hhmm(end)
    local = now.astimezone(KST)
    for day in (local.date(), local.date() - timedelta(days=1)):
        start = datetime(day.year, day.month, day.day, bh, bm, tzinfo=KST)
        finish = datetime(day.year, day.month, day.day, eh, em, tzinfo=KST)
        if finish <= start:
            finish += timedelta(days=1)
        if start <= now < finish:
            return start, finish
    return None


def regular_session_check(session, status, now, error=None):
    """Regular session only when ALL agree: the exchange calendar has a regular
    session at `now`, the broker reports 004/011 for the US market, and the broker's
    current segment contains `now` and equals the calendar open/close to the minute.

    A code alone (011 in particular) never establishes the regular session.
    `session` is Schedule.trading_session(now); `status` is Meritz.market_status()
    or None when that read failed (then `error` explains why)."""
    reasons = []
    report = {"regular": False,
              "calendar_session": session[0] if session else None,
              "calendar_open_kst": session[1].astimezone(KST).isoformat() if session else None,
              "calendar_close_kst": session[2].astimezone(KST).isoformat() if session else None,
              "broker_code": status.get("code") if status else None,
              "broker_begin_kst": status.get("begin_kst") if status else None,
              "broker_end_kst": status.get("end_kst") if status else None,
              "broker_segment_kst": None, "reasons": reasons,
              "unverified": list(REGULAR_SESSION_UNVERIFIED)}
    if session is None:
        reasons.append("Exchange calendar: no regular session now (holiday, weekend, pre/after-market or closed)")
    if status is None:
        reasons.append("Broker market-hours read failed" + (": " + error if error else ""))
        return report
    if status.get("market") not in (None, "01"):
        reasons.append("Broker market-hours response is not for the US market")
    code = status.get("code")
    if code not in REGULAR_CANDIDATE_CODES:
        reasons.append(f"Broker market code {code} is not a regular-session code")
    begin, end = status.get("begin_kst"), status.get("end_kst")
    if begin == "0000" and end == "0000":
        reasons.append("Broker segment times 0000/0000: market closed")
        return report
    try:
        segment = broker_segment(begin, end, now)
    except ValueError:
        reasons.append("Broker segment times are not valid KST HHMM")
        return report
    if segment is None:
        reasons.append("Current time is outside the broker's current segment")
        return report
    report["broker_segment_kst"] = [segment[0].isoformat(), segment[1].isoformat()]
    if session is not None and (segment[0] != session[1] or segment[1] != session[2]):
        reasons.append("Broker segment differs from the calendar regular session (DST/early close/pre/after-market mismatch)")
    report["regular"] = not reasons
    if report["regular"] and code == "011":
        report["note"] = "011 accepted only because its segment equals the calendar regular session (unverified)"
    return report


def confirm_regular_session(schedule, fetch_status, now):
    """Shared by run and doctor. Read failures are reported, never treated as open."""
    session = schedule.trading_session(now)
    try:
        status = fetch_status()
    except Stop as exc:
        return regular_session_check(session, None, now, str(exc))
    except Exception as exc:
        return regular_session_check(session, None, now, type(exc).__name__)
    return regular_session_check(session, status, now)
