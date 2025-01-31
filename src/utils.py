import time
import datetime
from exchange_calendars import get_calendar
import pytz

def get_time_until_market_open() -> datetime.timedelta:
    """
    Get the time remaining until the next market open.
    If market is already open, returns timedelta of 0.

    Returns:
        datetime.timedelta representing time until market open
    """
    nyse = get_calendar('XNYS')  # NYSE calendar
    ny_tz = pytz.timezone('America/New_York')
    current_time = datetime.datetime.now(ny_tz)
    next_open = nyse.next_open(current_time)
    delta = next_open - current_time

    if delta.total_seconds() <= 0:
        return datetime.timedelta(0)
    return delta


def is_market_open() -> bool:
    nyse = get_calendar('XNYS')  # NYSE calendar
    ny_tz = pytz.timezone('America/New_York')
    current_time = datetime.datetime.now(ny_tz)
    return nyse.is_open_on_minute(current_time)
