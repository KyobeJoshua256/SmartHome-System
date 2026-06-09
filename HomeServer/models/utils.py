from datetime import datetime, timedelta, timezone
from typing import Optional

EAT = timezone(timedelta(hours=3))

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
