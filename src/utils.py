import datetime
from exchange_calendars import get_calendar
from zoneinfo import ZoneInfo


def get_next_market_open() -> datetime.datetime:
    """Get the next market open time for NYSE with timezone information.

    Returns:
        A timezone-aware datetime object representing the next market open time.
    """
    nyse = get_calendar('XNYS')  # NYSE calendar
    current_time = datetime.datetime.now(ZoneInfo('America/New_York'))
    next_open = nyse.next_open(current_time)
    return next_open


def is_market_open() -> bool:
    nyse = get_calendar('XNYS')  # NYSE calendar
    current_time = datetime.datetime.now(ZoneInfo('America/New_York'))
    return nyse.is_open_on_minute(current_time)


def format_time_until(target_time: datetime.datetime) -> str:
    """Format the time until a future datetime"""
    time_until = target_time - datetime.datetime.now(target_time.tzinfo)
    if time_until.total_seconds() <= 0:
        return "now"

    # Format the time delta into a human-readable string
    days = time_until.days
    hours = time_until.seconds // 3600
    minutes = (time_until.seconds % 3600) // 60

    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    return " ".join(parts) if parts else "less than 1m"