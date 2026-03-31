"""
America/Chicago normalization for imports and Trade.date (calendar bucketing).
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
from django.utils import timezone

CHICAGO_TZ = ZoneInfo("America/Chicago")


def ensure_chicago_datetime(value) -> datetime:
    """
    Parse a CSV/pandas scalar into an aware datetime in America/Chicago.
    Naive datetimes are treated as Chicago wall time; aware datetimes are converted.
    """
    ts = pd.to_datetime(value)
    dt = ts.to_pydatetime()
    if timezone.is_naive(dt):
        return timezone.make_aware(dt, CHICAGO_TZ)
    return dt.astimezone(CHICAGO_TZ)


def trade_calendar_date(dt: datetime) -> date:
    """Calendar date in Chicago for Trade.date and journal alignment."""
    return ensure_chicago_datetime(dt).date()
