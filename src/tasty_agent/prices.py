import asyncio
from datetime import datetime
from typing import Literal
from decimal import Decimal
from tastytrade.streamer import DXLinkStreamer
from tastytrade.dxfeed import Quote

from .state import account_state
from .instrument import create_instrument

async def get_prices(
    underlying_symbol: str,
    expiration_date: str | None = None,
    option_type: Literal["C", "P"] | None = None,
    strike: float | None = None,
) -> tuple[Decimal, Decimal] | str:
    """Get current bid/ask prices for a stock or option.

    Args:
        underlying_symbol: Underlying symbol (e.g., "SPY", "AAPL")
        expiration_date: Optional expiration date in YYYY-MM-DD format for options
        option_type: Optional option type ("C" for call, "P" for put)
        strike: Optional strike price

    Returns:
        Tuple of (bid, ask) prices as Decimals if successful, or error message string if failed
    """
    try:
        # Convert expiration_date string to datetime if provided
        expiry_datetime = None
        if expiration_date:
            try:
                expiry_datetime = datetime.strptime(expiration_date, "%Y-%m-%d")
            except ValueError:
                return "Invalid expiration date format. Please use YYYY-MM-DD format"

        # Get instrument
        instrument = await create_instrument(
            underlying_symbol=underlying_symbol,
            expiration_date=expiry_datetime,
            option_type=option_type,
            strike=strike
        )
        if instrument is None:
            return f"Could not find instrument for symbol: {underlying_symbol}"

        # Get streamer symbol
        streamer_symbol = instrument.streamer_symbol
        if not streamer_symbol:
            return f"Could not get streamer symbol for {instrument.symbol}"

        # Get quote data
        try:
            async with DXLinkStreamer(account_state.session) as streamer:
                await streamer.subscribe(Quote, [streamer_symbol])
                quote = await asyncio.wait_for(streamer.get_event(Quote), timeout=10.0)
                return Decimal(str(quote.bid_price)), Decimal(str(quote.ask_price))
        except asyncio.TimeoutError:
            return f"Timed out waiting for quote data for {instrument.symbol}"

    except Exception as e:
        return f"Error fetching prices: {str(e)}" 