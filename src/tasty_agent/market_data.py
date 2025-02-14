from datetime import datetime
from typing import Literal
from decimal import Decimal
import asyncio
import logging
from tastytrade import metrics
from tastytrade.streamer import DXLinkStreamer
from tastytrade.dxfeed import Quote

from .instrument import get_instrument_for_symbol

logger = logging.getLogger(__name__)

class MarketDataService:
    """Service for fetching market data"""
    def __init__(self, session, account):
        self.session = session
        self.account = account
        self.streamer = None

    async def get_prices(
        self,
        symbol: str,
        expiration_date: datetime | None = None,
        option_type: Literal["C", "P"] | None = None,
        strike: float | None = None
    ) -> tuple[Decimal, Decimal]:
        """Get bid/ask prices for a symbol."""
        try:
            instrument = await get_instrument_for_symbol(
                symbol=symbol,
                expiration_date=expiration_date,
                option_type=option_type,
                strike=strike
            )
            if not instrument:
                raise ValueError(f"Could not get instrument for {symbol}")

            streamer_symbol = instrument.streamer_symbol
            if not streamer_symbol:
                raise ValueError(f"Could not get streamer symbol for {symbol}")

            async with DXLinkStreamer(self.session) as streamer:
                await streamer.subscribe(Quote, [streamer_symbol])
                quote = await asyncio.wait_for(streamer.get_event(Quote), timeout=10.0)
                return Decimal(str(quote.bid_price)), Decimal(str(quote.ask_price))

        except asyncio.TimeoutError:
            raise ValueError(f"Timed out waiting for quote data for {symbol}")
        except Exception as e:
            raise ValueError(f"Error fetching prices: {str(e)}")

    async def get_metrics(self, symbols: list[str]) -> list[metrics.MarketMetrics]:
        """Get market metrics for symbols."""
        return await metrics.a_get_market_metrics(self.session, symbols)
