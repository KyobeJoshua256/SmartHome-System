from datetime import datetime, timedelta, timezone
from typing import Optional
from datetime import datetime
import pytz
from sqlalchemy import Column, DateTime, event
from sqlalchemy.orm import DeclarativeBase

# Use pytz for timezone handling (more reliable than zoneinfo)
KAMPALA_TZ = pytz.timezone('Africa/Kampala')
EAT = KAMPALA_TZ
VALID_DAY_NAMES = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}


def now_kampala():
    return datetime.now(pytz.UTC).replace(microsecond=0)

def to_uganda():
    """Return current Kampala time stripped of microseconds."""
    # Use pytz to get current Kampala time
    return datetime.now(KAMPALA_TZ).replace(microsecond=0)


def to_uganda_time(dt: datetime) -> datetime:
    """Convert a datetime (naive or tz-aware) to Kampala time, stripped of microseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(KAMPALA_TZ).replace(microsecond=0)


class TimestampMixin:
    """Automatic timestamp tracking in Kampala time (EAT, UTC+3), no microseconds."""

    created_at = Column(
        DateTime(timezone=True),
        default=now_kampala,       
        nullable=False,
        index=True,
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=now_kampala,
        onupdate=now_kampala,      
        nullable=False,
    )