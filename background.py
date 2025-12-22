import asyncio
import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import typer
from tastytrade.market_sessions import ExchangeType, MarketStatus, a_get_market_sessions
from tastytrade.session import Session

from agent import create_tastytrader_agent  # loads .env internally

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


async def check_market_open() -> bool:
    """Check if NYSE market is currently open (async)."""
    try:
        client_secret = os.getenv("TASTYTRADE_CLIENT_SECRET")
        refresh_token = os.getenv("TASTYTRADE_REFRESH_TOKEN")
        if not client_secret or not refresh_token:
            logger.warning("Missing TastyTrade credentials for market check. Proceeding with agent run.")
            return True
        session = Session(client_secret, refresh_token)
        market_sessions = await a_get_market_sessions(session, [ExchangeType.NYSE])
        return any(ms.status == MarketStatus.OPEN for ms in market_sessions)
    except Exception as e:
        logger.warning(f"Failed to check market status: {e}. Proceeding with agent run.")
        return True

async def run_background_agent(instructions: str, period: int | None = None, schedule: datetime | None = None, market_open_only: bool = True):
    if schedule:
        sleep_seconds = (schedule - datetime.now()).total_seconds()
        if sleep_seconds > 0:
            await asyncio.sleep(sleep_seconds)

    agent = create_tastytrader_agent()
    async with agent:
        if period:
            last_result = None
            try:
                while True:
                    if market_open_only and not await check_market_open():
                        logger.info("Markets are closed, skipping agent run.")
                    else:
                        logger.info("Running agent...")
                        result = await agent.run(instructions)
                        last_result = result.output
                    await asyncio.sleep(period)
            except KeyboardInterrupt:
                pass
            if last_result:
                print(f"🤖 {last_result}")
        else:
            if market_open_only and not await check_market_open():
                logger.info("Markets are closed, skipping agent run.")
                return
            logger.info("Running agent...")
            result = await agent.run(instructions)
            print(f"🤖 {result.output}")

app = typer.Typer(help="Tasty Agent - Background Trading Bot")

@app.command()
def main(
    instructions: str = typer.Argument(..., help="Instructions for the agent"),
    schedule: str | None = typer.Option(None, "--schedule", "-s", help="Schedule time (e.g., '9:30am') in NYC timezone"),
    period: int | None = typer.Option(None, "--period", "-p", help="Period in seconds between runs (e.g., 1800 for 30min, 3600 for 1hr)"),
    hourly: bool = typer.Option(False, "--hourly", help="Run every hour (shorthand for --period 3600)"),
    daily: bool = typer.Option(False, "--daily", help="Run every day (shorthand for --period 86400)"),
    market_open: bool = typer.Option(False, "--market-open", help="Schedule for market open (shorthand for --schedule '9:30am')"),
    ignore_market_hours: bool = typer.Option(False, "--ignore-market-hours", help="Run even when markets are closed")
):
    # Handle schedule parsing
    schedule_time = None
    if market_open:
        schedule_str = "9:30am"
    elif schedule:
        schedule_str = schedule
    else:
        schedule_str = None

    if schedule_str:
        nyc_tz = ZoneInfo('America/New_York')
        for fmt in ('%I:%M%p', '%H:%M'):
            try:
                parsed_time = datetime.strptime(schedule_str.lower(), fmt).time()
                break
            except ValueError:
                continue
        else:
            typer.echo(f"Invalid time format: {schedule_str}. Use format like '9:30am' or '09:30'", err=True)
            raise typer.Exit(1)

        nyc_now = datetime.now(nyc_tz)
        schedule_time = datetime.combine(nyc_now.date(), parsed_time, nyc_tz)

        # If time has passed today, schedule for tomorrow
        if schedule_time <= nyc_now:
            schedule_time += timedelta(days=1)

        logger.info(f"Scheduled to run at: {schedule_time.strftime('%Y-%m-%d %I:%M %p %Z')}")

    # Determine period
    final_period = 3600 if hourly else 86400 if daily else period

    # Execute with period and/or schedule
    asyncio.run(run_background_agent(instructions, period=final_period, schedule=schedule_time, market_open_only=not ignore_market_hours))

if __name__ == "__main__":
    app()
