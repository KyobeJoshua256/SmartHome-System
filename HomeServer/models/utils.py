from datetime import datetime, timedelta, timezone
from typing import Optional
from datetime import datetime
from zoneinfo import ZoneInfo
from sqlalchemy import Column, DateTime, event
from sqlalchemy.orm import DeclarativeBase

KAMPALA_TZ = ZoneInfo("Africa/Kampala")
EAT = KAMPALA_TZ
VALID_DAY_NAMES = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}

def now_utc():
    """East Africa Time (UTC+3), consistent across all models."""
    return datetime.now(EAT)

def to_uganda_time(dt: Optional[datetime]) -> Optional[datetime]:
    """Convert *dt* (naive UTC or aware) to Uganda time (UTC+3)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(EAT)



def now_kampala():
    """Return current Kampala time stripped of microseconds."""
    return datetime.now(KAMPALA_TZ).replace(microsecond=0)

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