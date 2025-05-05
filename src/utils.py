import datetime
from exchange_calendars import get_calendar
from zoneinfo import ZoneInfo


def get_next_market_open() -> datetime.datetime:
    """Get the next market open time for NYSE with timezone information.

    Returns:
        A timezone-aware datetime object representing the next market open time.
    """
    nyse = get_calendar('XNYS')  # NYSE calendar
    # Get current time in the market's timezone to avoid ambiguity
    current_time_market_tz = datetime.datetime.now(ZoneInfo('America/New_York'))
    next_open = nyse.next_open(current_time_market_tz)
    return next_open


def is_market_open() -> bool:
    nyse = get_calendar('XNYS')  # NYSE calendar
    current_time = datetime.datetime.now(ZoneInfo('America/New_York'))
    return nyse.is_open_on_minute(current_time)


def format_time_until(target_time: datetime.datetime) -> str:
    """Format the time until a future datetime"""
    if target_time.tzinfo is None:
        now = datetime.datetime.now()
    else:
        now = datetime.datetime.now(target_time.tzinfo)

    time_until = target_time - now
    if time_until.total_seconds() <= 0:
        return "now"

    total_seconds = int(time_until.total_seconds())
    days = total_seconds // (24 * 3600)
    seconds_remaining_after_days = total_seconds % (24 * 3600)
    hours = seconds_remaining_after_days // 3600
    minutes = (seconds_remaining_after_days % 3600) // 60

    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    # Show minutes unless it's exactly 0 and there are larger units
    if minutes > 0 or not parts:
        parts.append(f"{minutes}m")

    return " ".join(parts) if parts else "less than 1m"