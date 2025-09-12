import argparse
import asyncio
import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from pydantic_ai import Agent
from pydantic_ai.mcp import MCPServerStdio
from tastytrade.market_sessions import a_get_market_sessions, ExchangeType, MarketStatus
from tastytrade.session import OAuthSession


load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

agent = Agent('openai:gpt-4o', toolsets=[MCPServerStdio(
    'uv', args=['run', 'tasty-agent', 'stdio'], timeout=60, env=os.environ
)])

async def check_market_open() -> bool:
    try:
        client_secret = os.getenv("TASTYTRADE_CLIENT_SECRET")
        refresh_token = os.getenv("TASTYTRADE_REFRESH_TOKEN")
        session = OAuthSession(client_secret, refresh_token)
        
        market_sessions = await a_get_market_sessions(session, [ExchangeType.NYSE])
        return any(market_session.status == MarketStatus.OPEN for market_session in market_sessions)
    except Exception as e:
        logger.warning(f"Failed to check market status: {e}. Proceeding with agent run.")
        return True

async def task(instructions: str, period: int = None, schedule: datetime = None, market_open_only: bool = True):
    if schedule:
        now = datetime.now()
        if schedule > now:
            sleep_seconds = (schedule - now).total_seconds()
            await asyncio.sleep(sleep_seconds)
    
    async def run_agent():
        if market_open_only and not await check_market_open():
            logger.info("Markets are closed, skipping agent run.")
            return
        
        logger.info("Running agent...")
        async with agent:
            if instructions:
                result = await agent.run(instructions)
                print(f"🤖 {result.output}")
    
    if period:
        try:
            while True:
                await run_agent()
                await asyncio.sleep(period)
        except KeyboardInterrupt:
            pass
    else:
        await run_agent()

async def main():
    parser = argparse.ArgumentParser(
        description='Tasty Agent - Background Trading Bot',
        epilog='''Examples:
  %(prog)s "Check my portfolio" --market-open
  %(prog)s "Analyze SPY options" --hourly
  %(prog)s "Generate daily report" --daily --ignore-market-hours
  %(prog)s "Monitor AAPL" --schedule "2:30pm" --period 900''',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument('instructions', type=str, help='Instructions for the agent')
    parser.add_argument('--schedule', type=str, help='Schedule time (e.g., "9:30am") in NYC timezone')
    parser.add_argument('--period', type=int, help='Period in seconds between runs (e.g., 1800 for 30min, 3600 for 1hr)')
    parser.add_argument('--hourly', action='store_true', help='Run every hour (shorthand for --period 3600)')
    parser.add_argument('--daily', action='store_true', help='Run every day (shorthand for --period 86400)')
    parser.add_argument('--market-open', action='store_true', help='Schedule for market open (shorthand for --schedule "9:30am")')
    parser.add_argument('--ignore-market-hours', action='store_true', help='Run even when markets are closed')
    args = parser.parse_args()
    
    instructions = args.instructions
    
    # Handle schedule parsing if provided
    schedule_time = None
    schedule_str = args.schedule
    
    # Handle market-open shorthand
    if args.market_open:
        schedule_str = "9:30am"
    
    if schedule_str:
        nyc_tz = ZoneInfo('America/New_York')
        try:
            parsed_time = datetime.strptime(schedule_str.lower(), '%I:%M%p').time()
        except ValueError:
            try:
                parsed_time = datetime.strptime(schedule_str.lower(), '%H:%M').time()
            except ValueError:
                logger.error(f"Invalid time format: {schedule_str}. Use format like '9:30am' or '09:30'")
                return
        
        nyc_now = datetime.now(nyc_tz)
        schedule_time = nyc_tz.localize(datetime.combine(nyc_now.date(), parsed_time))
        
        # If time has passed today, schedule for tomorrow
        if schedule_time <= nyc_now:
            schedule_time += timedelta(days=1)
        
        logger.info(f"Scheduled to run at: {schedule_time.strftime('%Y-%m-%d %I:%M %p %Z')}")
    
    # Determine period (flags take precedence over --period)
    period = args.period
    if args.hourly:
        period = 3600
    elif args.daily:
        period = 86400
    
    # Execute with period and/or schedule
    await task(instructions, period=period, schedule=schedule_time, market_open_only=not args.ignore_market_hours)

if __name__ == '__main__':
    asyncio.run(main())