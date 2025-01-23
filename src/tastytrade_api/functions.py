import logging
from decimal import Decimal
from datetime import date, datetime, timedelta

from .models import Balances, Position, OptionExpirationIV, EarningsData, RelevantMarketMetric

logger = logging.getLogger(__name__)

# Maximum percentage of net liquidating value for any single position
MAX_POSITION_SIZE_PCT = 0.40  # 40%


def get_option_occ_symbol(option_desc: str) -> str:
    """Convert option description (e.g. 'SPY 150C 2025-01-19') to OCC symbol"""
    ticker, strike_type, date_str = option_desc.split()
    price = float(strike_type[:-1])
    option_type = strike_type[-1]

    return (
        f"{ticker.upper().ljust(6)}"
        f"{datetime.strptime(date_str, '%Y-%m-%d').strftime('%y%m%d')}"
        f"{option_type}"
        f"{int(price * 1000):08d}"
    )

def get_option_streamer_symbol(option_desc: str) -> str:
    """Convert option description (e.g. 'SPY 150C 2025-01-19') to dxfeed streamer symbol"""
    from tastytrade.instruments import Option  # Local import
    occ_symbol = get_option_occ_symbol(option_desc)
    return Option.occ_to_streamer_symbol(occ_symbol)

def get_option_desc(occ_symbol: str) -> str:
    """Convert OCC symbol to option description format (e.g. 'SPY   240119C00150000' to 'SPY 150C 2024-01-19')"""
    # Extract components from OCC symbol
    ticker = occ_symbol[:6].strip()
    exp_date = datetime.strptime(occ_symbol[6:12], '%y%m%d')
    option_type = occ_symbol[12]
    strike_price = int(occ_symbol[13:]) / 1000

    return f"{ticker} {strike_price:g}{option_type} {exp_date.strftime('%Y-%m-%d')}"

def get_instrument_for_symbol(symbol: str, session):
    """
    Helper to fetch an Option or Equity depending on whether
    the symbol looks like an option description (contains a space)
    or a standard ticker.
    """
    from tastytrade.instruments import Equity, Option  # Local import

    if ' ' in symbol:  # Option description (e.g. "SPY 150C 2025-01-19")
        occ = get_option_occ_symbol(symbol)
        return Option.get_option(session, occ)
    else:  # Equity ticker (e.g. "SPY")
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
    from tastytrade import metrics
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
        symbol: Either a stock symbol (e.g. "SPY") or option description ("SPY 150C 2025-01-19")

    Returns:
        tuple[Decimal, Decimal]: The (bid_price, ask_price) for the instrument

    Raises:
        TimeoutError: If no quote is received within 10 seconds; review the symbol and try again
    """
    from tastytrade.dxfeed import Quote
    from tastytrade import DXLinkStreamer
    import asyncio

    # Convert to streamer symbol format if it's an option
    if ' ' in symbol:
        streamer_symbol = get_option_streamer_symbol(symbol)
    else:
        streamer_symbol = symbol.upper()

    async with DXLinkStreamer(session) as streamer:
        await streamer.subscribe(Quote, [streamer_symbol])

        try:
            # Wait for quote with 10 second timeout
            quote = await asyncio.wait_for(streamer.get_event(Quote), timeout=10.0)
            return Decimal(str(quote.bid_price)), Decimal(str(quote.ask_price))
        except asyncio.TimeoutError:
            raise TimeoutError(f"Timed out waiting for quote data for symbol: {symbol}")

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
    min_excess_capital: int = 1000,
) -> str:
    """Internal helper that places an order and adjusts the price until filled."""
    import asyncio
    from tastytrade.order import (
        NewOrder,
        InstrumentType,
        OrderAction,
        OrderTimeInForce,
        OrderType,
        OrderStatus,
        Leg
    )
    from tastytrade.instruments import Option
    from decimal import Decimal

    logger.info(
        "Attempting to place order: symbol=%r, quantity=%r, initial_price=%r, action=%r, dry_run=%r",
        symbol,
        quantity,
        price,
        action,
        dry_run
    )

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
        
        orders = account.get_orders(session)
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
        symbol: Either a stock symbol (e.g. "SPY") or option description ("INTC 150C 2025-01-19")
        quantity: Number of shares or contracts
        price: Price to buy at
        dry_run: If True, simulates the order without executing it

    Returns:
        str: Success message, warnings, and errors if any
    """
    from tastytrade.order import OrderAction
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
    """Sell to close an existing position (stock or option)."""
    from tastytrade.order import OrderAction
    instrument = get_instrument_for_symbol(symbol, session)
    occ_symbol = instrument.symbol if instrument else symbol

    positions = await get_positions(session, account)
    position = next((p for p in positions if p.symbol == occ_symbol), None)
    if not position:
        return "Position not found"

    # Make sure we don't sell more than we have
    quantity = min(quantity, position.quantity)

    return await _place_order(
        session,
        account,
        occ_symbol,
        quantity,
        price,
        OrderAction.SELL_TO_CLOSE,
        dry_run=dry_run
    )