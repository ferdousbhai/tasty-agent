import asyncio
import logging
from decimal import Decimal
from datetime import date, datetime, timedelta

from tastytrade import DXLinkStreamer, metrics
from tastytrade.dxfeed import Quote
from tastytrade.instruments import Option, Equity, OptionType, get_option_chain
from tastytrade.order import (
    InstrumentType,
    NewOrder,
    OrderAction,
    OrderStatus,
    OrderTimeInForce,
    OrderType,
    Leg
)

from .models import Balances, Position, OptionExpirationIV, EarningsData, RelevantMarketMetric

logger = logging.getLogger(__name__)

# Maximum percentage of net liquidating value for any single position
MAX_POSITION_SIZE_PCT = 0.40  # 40%

# Common symbol formats accepted throughout this module:
# - Stock symbol (e.g. "SPY")
# - Option description (e.g. "SPY 150C 2025-01-19" or "SPY 150 C 2025-01-19")
# - OCC symbol (e.g. "SPY250119C00150000")

def get_instrument_for_symbol(symbol: str, session) -> Option | Equity:
    """
    Get an instrument (Option or Equity) from a symbol or option description.

    Args:
        symbol: Can be any of:
            - A stock symbol (e.g. "SPY")
            - A string description (e.g. "SPY 150C 2025-01-19" or "SPY 150 C 2025-01-19")
            - An OCC symbol (e.g. "SPY250119C00150000")
    """
    # Try to parse as OCC symbol first (e.g. "SPY250119C00150000")
    if len(symbol) > 6 and symbol[12] in ['C', 'P']:  # Min length for OCC symbol and has C/P
        try:
            # For OCC symbols, use Option.get_option() directly
            return Option.get_option(session, symbol)
        except Exception:
            # Fall through if OCC parsing fails
            pass

    # Try to parse as option description (e.g. "SPY 150C 2025-01-19" or "SPY 150 C 2025-01-19")
    parts = symbol.strip().split()
    if len(parts) >= 3:
        try:
            ticker = parts[0]
            # Handle cases with or without space between strike and option type
            if len(parts) == 3:
                strike_str = parts[1][:-1]  # Remove C/P from strike
                option_type = parts[1][-1].upper()
                exp_date_str = parts[2]
            else:  # len(parts) == 4
                strike_str = parts[1]
                option_type = parts[2].upper()
                exp_date_str = parts[3]

            if option_type not in ['C', 'P']:
                raise ValueError("Option type must be 'C' or 'P'")

            strike = float(strike_str)
            exp_date = datetime.strptime(exp_date_str, "%Y-%m-%d").date()

            # Get the option chain and find our specific option
            chain = get_option_chain(session, ticker)

            # Find the expiration date in the chain
            if exp_date not in chain:
                raise ValueError(f"No options found for expiration date {exp_date}")

            # Find the option with matching strike and type
            for option in chain[exp_date]:
                if (option.strike_price == Decimal(str(strike)) and 
                    option.option_type == (OptionType.CALL if option_type == 'C' else OptionType.PUT)):
                    return option

            raise ValueError(f"No option found for strike {strike} and type {option_type}")

        except (ValueError, IndexError) as e:
            if "Option type must be" in str(e):
                raise
            # Fall through to equity if option parsing fails

    # Regular equity symbol
    return Equity.get_equity(session, symbol)

async def get_balances(session, account) -> Balances:
    balances = await account.a_get_balances(session)
    return Balances(
        cash_balance=balances.cash_balance,
        buying_power=balances.derivative_buying_power,
        net_liquidating_value=balances.net_liquidating_value,
        maintenance_excess=balances.maintenance_excess
    )

async def get_positions(session, account) -> list[Position]:
    current_positions = await account.a_get_positions(session)
    return [
        Position(
            symbol=position.symbol,
            instrument_type=position.instrument_type,
            underlying_symbol=position.underlying_symbol,
            quantity=position.quantity,
            quantity_direction=position.quantity_direction,
            value=position.quantity * position.multiplier * position.close_price
        )
        for position in current_positions
    ]

def get_transactions(session, account, start_date: str | None = None):
    """Get transaction history starting from a specific date.

    Args:
        start_date (str, optional): Date string in YYYY-MM-DD format (e.g., "2024-01-01")
        If not provided, defaults to 90 days ago
    """
    if start_date is None:
        # Default to 90 days ago
        date_obj = date.today() - timedelta(days=90)
    else:
        # Convert string date to date object
        try:
            date_obj = datetime.strptime(start_date, "%Y-%m-%d").date()
        except ValueError:
            raise ValueError("start_date must be in YYYY-MM-DD format (e.g., '2024-01-01')")

    history = account.get_history(session, start_date=date_obj)

    return history


async def get_market_metrics(session, symbols: list[str]) -> list[RelevantMarketMetric]:
    """
    Get reduced market metrics for a list of symbols, returning only
    fields that are particularly relevant for analysts when deciding to trade.
    """
    raw_metrics = await metrics.a_get_market_metrics(session, symbols)

    results: list[RelevantMarketMetric] = []

    for rm in raw_metrics:
        # Convert the option expirations
        expirations = [
            OptionExpirationIV(
                expiration_date=ov.expiration_date,
                implied_volatility=ov.implied_volatility
            )
            for ov in (rm.option_expiration_implied_volatilities or [])
        ]

        # Convert the earnings data
        earnings_data = None
        if rm.earnings:
            earnings_data = EarningsData(
                expected_report_date=rm.earnings.expected_report_date,
                actual_eps=rm.earnings.actual_eps,
                consensus_estimate=rm.earnings.consensus_estimate,
                time_of_day=rm.earnings.time_of_day
            )

        # Build our relevant metric model
        metric = RelevantMarketMetric(
            symbol=rm.symbol,
            implied_volatility_index=rm.implied_volatility_index,
            implied_volatility_index_rank=Decimal(rm.implied_volatility_index_rank) if rm.implied_volatility_index_rank else None,
            implied_volatility_percentile=Decimal(rm.implied_volatility_percentile) if rm.implied_volatility_percentile else None,
            liquidity_rating=rm.liquidity_rating,
            updated_at=rm.updated_at,
            option_expiration_implied_volatilities=expirations,
            beta=rm.beta,
            corr_spy_3month=rm.corr_spy_3month,
            market_cap=rm.market_cap,
            implied_volatility_30_day=rm.implied_volatility_30_day,
            historical_volatility_30_day=rm.historical_volatility_30_day,
            historical_volatility_60_day=rm.historical_volatility_60_day,
            historical_volatility_90_day=rm.historical_volatility_90_day,
            iv_hv_30_day_difference=rm.iv_hv_30_day_difference,
            earnings=earnings_data
        )
        results.append(metric)

    return results

async def get_bid_ask_price(session, symbol: str) -> tuple[Decimal, Decimal]:
    """Get the current bid and ask price for a given symbol.

    Args:
        symbol: Can be any of:
            - A stock symbol (e.g. "SPY")
            - A string description (e.g. "SPY 150C 2025-01-19")
            - An OCC symbol (e.g. "SPY250119C00150000")

    Returns:
        tuple[Decimal, Decimal]: The (bid_price, ask_price) for the instrument

    Raises:
        TimeoutError: If no quote is received within 10 seconds; review the symbol and try again
    """
    instrument = get_instrument_for_symbol(symbol, session)

    streamer_symbol = instrument.streamer_symbol or instrument.symbol
    logger.info("Using streamer symbol: %s", streamer_symbol)

    async with DXLinkStreamer(session) as streamer:
        await streamer.subscribe(Quote, [streamer_symbol])

        try:
            # Wait for quote with 10 second timeout
            quote = await asyncio.wait_for(streamer.get_event(Quote), timeout=10.0)
            return Decimal(str(quote.bid_price)), Decimal(str(quote.ask_price))
        except asyncio.TimeoutError:
            raise TimeoutError(f"Timed out waiting for quote data for symbol: {streamer_symbol}")

async def _place_order(
    session,
    account,
    symbol: str,
    quantity: int,
    price: float,
    action: str,
    dry_run: bool = True,
    check_interval: float = 15.0,
    max_attempts: int = 20,
    min_excess_capital: int = 900,
) -> str:
    """Internal helper that places an order and adjusts the price until filled.

    Args:
        symbol: Either:
            - A stock symbol (e.g. "SPY")
            - A string description (e.g. "SPY 150C 2025-01-19")
    """
    instrument = get_instrument_for_symbol(symbol, session)
    if not instrument:
        return "Instrument not found. Cannot place order."

    multiplier = instrument.shares_per_contract if isinstance(instrument, Option) else 1
    logger.info("Instrument symbol=%r, instrument type=%s, multiplier=%r",
                instrument.symbol, type(instrument).__name__, multiplier)

    # Check buying power and position size limits for buy orders
    if action == OrderAction.BUY_TO_OPEN:
        balances = await get_balances(session, account)
        order_value = Decimal(str(price)) * Decimal(str(quantity)) * Decimal(multiplier)
        max_value = min(
            balances.buying_power,
            balances.net_liquidating_value * Decimal(str(MAX_POSITION_SIZE_PCT))
        )
        logger.info("Calculated order_value=%s against max_value=%s", order_value, max_value)

        if order_value > max_value:
            quantity = int((max_value - min_excess_capital) / (Decimal(str(price)) * multiplier))
            logger.info("Reduced quantity from %s to %s based on max_value.", quantity, quantity)
            if quantity <= 0:
                logger.error("Order rejected: Exceeds available funds or position size limits.")
                return "Order rejected: Exceeds available funds or position size limits"

    # Create the order leg
    if action == OrderAction.BUY_TO_OPEN:
        leg = Leg(
            instrument_type=InstrumentType.EQUITY_OPTION if isinstance(instrument, Option) else InstrumentType.EQUITY,
            symbol=instrument.symbol,
            action=action,
            quantity=quantity
        )
    else:
        leg = instrument.build_leg(quantity, action)

    # Determine price direction multiplier (-1 for buys, 1 for sells)
    price_multiplier = -1 if action in (OrderAction.BUY_TO_OPEN, OrderAction.BUY_TO_CLOSE) else 1

    # Create and place initial order
    initial_order = NewOrder(
        time_in_force=OrderTimeInForce.DAY,
        order_type=OrderType.LIMIT,
        legs=[leg],
        price=Decimal(str(price)) * price_multiplier
    )

    response = account.place_order(session, initial_order, dry_run=dry_run)
    if response.errors:
        return "Order failed with errors:\n" + "\n".join(str(error) for error in response.errors)

    if dry_run:
        return "Dry run successful" + (
            "\nWarnings:\n" + "\n".join(str(w) for w in response.warnings) if response.warnings else ""
        )

    # Monitor and adjust order
    current_order = response.order
    for attempt in range(max_attempts):
        await asyncio.sleep(check_interval)

        orders = account.get_live_orders(session)
        order = next((o for o in orders if o.id == current_order.id), None)

        if not order:
            return "Order not found during monitoring"

        if order.status == OrderStatus.FILLED:
            return "Order filled successfully"

        if order.status not in (OrderStatus.LIVE, OrderStatus.RECEIVED):
            return f"Order in unexpected status: {order.status}"

        # Adjust price
        price_delta = 0.01 if action in (OrderAction.BUY_TO_OPEN, OrderAction.BUY_TO_CLOSE) else -0.01
        new_price = float(order.price) + price_delta
        logger.info("Adjusting order price from %s to %s (attempt %d)", order.price, new_price, attempt + 1)

        # Replace order with new price
        new_order = NewOrder(
            time_in_force=OrderTimeInForce.DAY,
            order_type=OrderType.LIMIT,
            legs=[leg],
            price=Decimal(str(new_price)) * price_multiplier
        )

        response = account.replace_order(session, order.id, new_order)
        if response.errors:
            return f"Failed to adjust order: {response.errors}"

        current_order = response.order

    return f"Order not filled after {max_attempts} price adjustments"

async def buy_to_open(
    session,
    account,
    symbol: str,
    quantity: int,
    price: float,
    dry_run: bool = True,
) -> str:
    """Buy to open a new stock or option position.

    Args:
        symbol: Either:
            - A stock symbol (e.g. "SPY")
            - A string description (e.g. "SPY 150C 2025-01-19")
        quantity: Number of shares or contracts
        price: Price to buy at
        dry_run: If True, simulates the order without executing it

    Returns:
        str: Success message, warnings, and errors if any
    """
    return await _place_order(
        session,
        account,
        symbol,
        quantity,
        price,
        OrderAction.BUY_TO_OPEN,
        dry_run=dry_run
    )

async def sell_to_close(
    session,
    account,
    symbol: str,
    quantity: int,
    price: float,
    dry_run: bool = True,
) -> str:
    """Sell to close an existing position (stock or option).

    Args:
        symbol: Either:
            - A stock symbol (e.g. "SPY")
            - A string description (e.g. "SPY 150C 2025-01-19")
        quantity: Number of shares or contracts
        price: Price to sell at
        dry_run: If True, simulates the order without executing it

    Returns:
        str: Success message, warnings, and errors if any
    """
    instrument = get_instrument_for_symbol(symbol, session)

    # Get current positions and orders
    positions = await get_positions(session, account)
    position = next((p for p in positions if p.symbol == instrument.symbol), None)
    if not position:
        return f"Error: No open position found for {instrument.symbol}"

    # Get existing orders to check for pending sell orders
    orders = account.get_live_orders(session)  # Changed from get_orders() to get_live_orders()
    pending_sell_quantity = sum(
        sum(leg.quantity for leg in order.legs)
        for order in orders
        if (order.status in (OrderStatus.LIVE, OrderStatus.RECEIVED) and
            any(leg.symbol == instrument.symbol and 
                leg.action == OrderAction.SELL_TO_CLOSE 
                for leg in order.legs))
    )

    # Calculate available quantity to sell (position size minus pending sells)
    available_quantity = position.quantity - pending_sell_quantity
    logger.info("Position quantity=%d, pending sell quantity=%d, available=%d",
                position.quantity, pending_sell_quantity, available_quantity)

    if available_quantity <= 0:
        return (f"Error: Cannot place order - entire position of {position.quantity} "
                f"already has pending sell orders")

    # Adjust requested quantity if it exceeds available
    quantity = min(quantity, available_quantity)
    if quantity <= 0:
        return f"Error: Position quantity ({available_quantity}) insufficient for requested sale"

    return await _place_order(
        session,
        account,
        symbol,
        quantity,
        price,
        OrderAction.SELL_TO_CLOSE,
        dry_run=dry_run
    )