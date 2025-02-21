import datetime
from exchange_calendars import get_calendar
from zoneinfo import ZoneInfo

def get_time_until_market_open() -> datetime.timedelta:
    """
    Get the time remaining until the next market open.
    If market is already open, returns timedelta of 0.

    Returns:
        datetime.timedelta representing time until market open
    """
    nyse = get_calendar('XNYS')  # NYSE calendar
    current_time = datetime.datetime.now(ZoneInfo('America/New_York'))
    next_open = nyse.next_open(current_time)
    delta = next_open - current_time

    if delta.total_seconds() <= 0:
        return datetime.timedelta(0)
    return delta


def is_market_open() -> bool:
    nyse = get_calendar('XNYS')  # NYSE calendar
    current_time = datetime.datetime.now(ZoneInfo('America/New_York'))
    return nyse.is_open_on_minute(current_time)


def format_time_delta(delta) -> str:
    """Format a timedelta into a human-readable string"""
    days = delta.days
    hours = delta.seconds // 3600
    minutes = (delta.seconds % 3600) // 60

    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    return " ".join(parts) if parts else "less than 1m"