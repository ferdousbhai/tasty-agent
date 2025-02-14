import logging
from datetime import datetime
from typing import Literal

from tastytrade.instruments import Option, Equity, NestedOptionChain

from .common import session

logger = logging.getLogger(__name__)

async def get_instrument_for_symbol(
    symbol: str,
    expiration_date: datetime | None = None,
    option_type: Literal["C", "P"] | None = None,
    strike: float | None = None,
) -> Option | Equity | None:
    """Get the instrument object for a given symbol.

    Args:
        symbol: Underlying symbol (e.g., "SPY", "AAPL")
        expiration_date: Optional expiration date for options
        option_type: Optional option type ("C" for call, "P" for put)
        strike: Optional strike price
        session: TastyTrade session

    Returns:
        Option or Equity instrument, or None if not found
    """
    try:
        # If no option parameters, treat as equity
        if not any([expiration_date, option_type, strike]):
            return Equity.get_equity(session, symbol)

        # Validate all option parameters are present
        if not all([expiration_date, option_type, strike]):
            logger.error("Must provide all option parameters (expiration_date, option_type, strike) or none")
            return None

        # Get option chain
        try:
            chain = NestedOptionChain.get_chain(session, symbol)

            # Find matching expiration
            exp_date = expiration_date.date()
            expiration = next(
                (exp for exp in chain.expirations 
                if exp.expiration_date == exp_date),
                None
            )
            if not expiration:
                logger.error(f"No expiration found for date {exp_date}")
                return None

            # Find matching strike
            strike_obj = next(
                (s for s in expiration.strikes 
                if float(s.strike_price) == strike),
                None
            )
            if not strike_obj:
                logger.error(f"No strike found for {strike}")
                return None

            # Get option symbol based on type
            option_symbol = strike_obj.call if option_type == "C" else strike_obj.put
            return Option.get_option(session, option_symbol)

        except Exception as e:
            logger.error(f"Error getting option chain: {e}")
            return None

    except Exception as e:
        logger.error(f"Error getting instrument for {symbol}: {e}")
        return None
