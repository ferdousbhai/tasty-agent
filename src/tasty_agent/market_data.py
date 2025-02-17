from decimal import Decimal
import asyncio
import logging
from tastytrade import metrics
from tastytrade.instruments import Option, Equity
from tastytrade.streamer import DXLinkStreamer
from tastytrade.dxfeed import Quote


logger = logging.getLogger(__name__)

async def get_prices(
    session,
    instrument: Option | Equity,
) -> tuple[Decimal, Decimal]:
    """Get bid/ask prices for an instrument."""
    try:
        streamer_symbol = instrument.streamer_symbol
        if not streamer_symbol:
            raise ValueError(f"Could not get streamer symbol for {instrument.symbol}")

        async with DXLinkStreamer(session) as streamer:
            await streamer.subscribe(Quote, [streamer_symbol])
            quote = await asyncio.wait_for(streamer.get_event(Quote), timeout=10.0)
            return Decimal(str(quote.bid_price)), Decimal(str(quote.ask_price))

    except asyncio.TimeoutError:
        raise ValueError(f"Timed out waiting for quote data for {instrument.symbol}")
    except Exception as e:
        raise ValueError(f"Error fetching prices: {str(e)}")

async def get_metrics(session, symbols: list[str]) -> list[metrics.MarketMetricInfo]:
    """Get market metrics for symbols."""
    return await metrics.a_get_market_metrics(session, symbols)