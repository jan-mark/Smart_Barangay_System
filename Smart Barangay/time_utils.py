from datetime import date, datetime
from zoneinfo import ZoneInfo


PHILIPPINE_TIME_ZONE = ZoneInfo("Asia/Manila")


def manila_now() -> datetime:
    return datetime.now(PHILIPPINE_TIME_ZONE)


def manila_today() -> date:
    return manila_now().date()


def manila_month_key(value: date | datetime | str | None) -> str:
    """Return a YYYY-MM key after interpreting a value in Philippine time."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        timestamp = value
    elif isinstance(value, date):
        return value.strftime("%Y-%m")
    else:
        raw_value = str(value).strip()
        if not raw_value:
            return ""
        try:
            timestamp = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
        except ValueError:
            try:
                return date.fromisoformat(raw_value[:10]).strftime("%Y-%m")
            except ValueError:
                return ""

    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=PHILIPPINE_TIME_ZONE)
    else:
        timestamp = timestamp.astimezone(PHILIPPINE_TIME_ZONE)
    return timestamp.strftime("%Y-%m")
