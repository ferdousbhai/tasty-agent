import logging
from datetime import datetime
from typing import Literal

from tastytrade.instruments import Option, Equity, NestedOptionChain

from .state import account_state

logger = logging.getLogger(__name__)

async def create_instrument(
    underlying_symbol: str,
    expiration_date: datetime | None = None,
    option_type: Literal["C", "P"] | None = None,
    strike: float | None = None,
) -> Option | Equity | None:
    """Create an instrument object for a given symbol.

    Args:
        underlying_symbol: Underlying symbol (e.g., "SPY", "AAPL")
        expiration_date: Optional expiration date for options
        option_type: Optional option type ("C" for call, "P" for put)
        strike: Optional strike price

    Returns:
        Option or Equity instrument, or None if not found
    """
    try:
        # If no option parameters, treat as equity
        if not any([expiration_date, option_type, strike]):
            return Equity.get_equity(account_state.session, underlying_symbol)

        # Validate all option parameters are present
        if not all([expiration_date, option_type, strike]):
            logger.error("Must provide all option parameters (expiration_date, option_type, strike) or none")
            return None

        try:
            # Get option chain
            chain: list[NestedOptionChain] = NestedOptionChain.get_chain(account_state.session, underlying_symbol)

            if not chain:
                logger.error(f"No option chain found for {underlying_symbol}")
                return None

            option_chain = chain[0]

            # Find matching expiration
            exp_date = expiration_date.date()
            expiration = next(
                (exp for exp in option_chain.expirations 
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
            return Option.get_option(account_state.session, option_symbol)

        except Exception as e:
            logger.error(f"Error getting option chain: {e}")
            return None

    except Exception as e:
        logger.error(f"Error getting instrument for {underlying_symbol}: {e}")
        return None