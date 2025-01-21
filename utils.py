import time
import datetime
from exchange_calendars import get_calendar
import pytz

def sleep_until_market_open() -> None:
    """
    Sleep until the next market open time using exchange_calendars.
    If the market is already open, this returns immediately.
    """
    nyse = get_calendar('XNYS')  # NYSE calendar
    ny_tz = pytz.timezone('America/New_York')

    while True:
        current_time = datetime.datetime.now(ny_tz)
        # next_open might be same day or next day
        next_open = nyse.next_open(current_time)

        delta = next_open - current_time
        if delta.total_seconds() <= 0:
            # Market is open or we are past that open time
            break

        hours, remainder = divmod(delta.seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        print(f"\rWaiting for market open: {hours:02d}:{minutes:02d}:{seconds:02d}", end="", flush=True)
        time.sleep(1)

    print("\nMarket is now open!")
    for i in range(10, 0, -1):
        print(f"\rReady to execute orders in {i:02d} seconds...", end="", flush=True)
        time.sleep(1)
    print()


def is_market_open() -> bool:
    nyse = get_calendar('XNYS')  # NYSE calendar
    ny_tz = pytz.timezone('America/New_York')
    current_time = datetime.datetime.now(ny_tz)
    return nyse.is_open_on_minute(current_time)
