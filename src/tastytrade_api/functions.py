import asyncio
import logging
from decimal import Decimal
from datetime import date, datetime, timedelta
from typing import Literal

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

    return account.get_history(session, start_date=date_obj)

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
    """Get the current bid and ask price for a given symbol."""
    instrument = get_instrument_for_symbol(symbol, session)

    streamer_symbol = instrument.streamer_symbol or instrument.symbol

    async with DXLinkStreamer(session) as streamer:
        await streamer.subscribe(Quote, [streamer_symbol])

        try:
            quote = await asyncio.wait_for(streamer.get_event(Quote), timeout=10.0)
            bid, ask = Decimal(str(quote.bid_price)), Decimal(str(quote.ask_price))
            return bid, ask
        except asyncio.TimeoutError:
            raise TimeoutError(f"Timed out waiting for quote data for symbol: {streamer_symbol}")

async def place_trade(
    session,
    account,
    symbol: str,
    quantity: int,
    action: Literal["Buy to Open", "Sell to Close"],
    mcp,
    dry_run: bool = True,
    check_interval: float = 15.0,
    max_attempts: int = 20,
) -> str:
    """Place a trade order (buy to open or sell to close).

    Args:
        session: TastyTrade session
        account: TastyTrade account
        symbol: Trading symbol
        quantity: Number of shares/contracts
        action: Trade action - either "Buy to Open" or "Sell to Close"
        mcp: MCP instance for logging to Claude Desktop
        dry_run: If True, simulates the order
        check_interval: Seconds to wait between price adjustment attempts
        max_attempts: Maximum number of price adjustments to attempt
    """
    instrument = get_instrument_for_symbol(symbol, session)
    if not instrument:
        mcp.send_log_message(level="error", data=f"Instrument not found for symbol: {symbol}")
        return "Instrument not found. Cannot place order."

    # Get current price
    try:
        bid, ask = await get_bid_ask_price(session, symbol)
        # Use ask price for buying, bid price for selling
        price = float(ask if action == "Buy to Open" else bid)
    except Exception as e:
        error_msg = f"Failed to get price for {symbol}: {str(e)}"
        mcp.send_log_message(level="error", data=error_msg)
        return error_msg

    if action == "Buy to Open":
        multiplier = instrument.shares_per_contract if isinstance(instrument, Option) else 1
        
        # Check buying power and position size limits
        balances = await get_balances(session, account)
        order_value = Decimal(str(price)) * Decimal(str(quantity)) * Decimal(multiplier)
        max_value = min(
            balances.buying_power,
            balances.net_liquidating_value * Decimal(str(MAX_POSITION_SIZE_PCT))
        )

        mcp.send_log_message(
            level="info",
            data=f"Order value: ${order_value:,.2f}, Max allowed: ${max_value:,.2f}"
        )

        if order_value > max_value:
            original_quantity = quantity
            quantity = int(max_value / (Decimal(str(price)) * multiplier))
            mcp.send_log_message(
                level="warning",
                data=f"Reduced order quantity from {original_quantity} to {quantity} due to position limits"
            )
            if quantity <= 0:
                mcp.send_log_message(
                    level="error",
                    data="Order rejected: Exceeds available funds or position size limits"
                )
                return "Order rejected: Exceeds available funds or position size limits"

    else:  # Sell to Close
        # Get current positions and orders
        positions = await get_positions(session, account)
        position = next((p for p in positions if p.symbol == instrument.symbol), None)
        if not position:
            mcp.send_log_message(
                level="error",
                data=f"No open position found for {instrument.symbol}"
            )
            return f"Error: No open position found for {instrument.symbol}"

        # Get existing orders to check for pending sell orders
        orders = account.get_live_orders(session)
        pending_sell_quantity = sum(
            sum(leg.quantity for leg in order.legs)
            for order in orders
            if (order.status in (OrderStatus.LIVE, OrderStatus.RECEIVED) and
                any(leg.symbol == instrument.symbol and
                    leg.action == OrderAction.SELL_TO_CLOSE
                    for leg in order.legs))
        )

        # Calculate available quantity to sell
        available_quantity = position.quantity - pending_sell_quantity
        mcp.send_log_message(
            level="info",
            data=f"Position: {position.quantity}, Pending sells: {pending_sell_quantity}, Available: {available_quantity}"
        )

        if available_quantity <= 0:
            error_msg = (f"Cannot place order - entire position of {position.quantity} "
                        f"already has pending sell orders")
            mcp.send_log_message(level="error", data=error_msg)
            return f"Error: {error_msg}"

        # Adjust requested quantity if it exceeds available
        if quantity > available_quantity:
            mcp.send_log_message(
                level="warning",
                data=f"Reducing sell quantity from {quantity} to {available_quantity} (maximum available)"
            )
            quantity = available_quantity

        if quantity <= 0:
            error_msg = f"Position quantity ({available_quantity}) insufficient for requested sale"
            mcp.send_log_message(level="error", data=error_msg)
            return f"Error: {error_msg}"

    # Create the order leg
    order_action = OrderAction.BUY_TO_OPEN if action == "Buy to Open" else OrderAction.SELL_TO_CLOSE
    if action == "Buy to Open":
        leg = Leg(
            instrument_type=InstrumentType.EQUITY_OPTION if isinstance(instrument, Option) else InstrumentType.EQUITY,
            symbol=instrument.symbol,
            action=order_action,
            quantity=quantity
        )
    else:  # Sell to Close
        leg = instrument.build_leg(quantity, order_action)

    # Create and place initial order
    mcp.send_log_message(
        level="info",
        data=f"Placing initial order: {action} {quantity} {symbol} @ ${price:.2f}"
    )

    initial_order = NewOrder(
        time_in_force=OrderTimeInForce.DAY,
        order_type=OrderType.LIMIT,
        legs=[leg],
        price=Decimal(str(price)) * (-1 if action == "Buy to Open" else 1)
    )

    response = account.place_order(session, initial_order, dry_run=dry_run)
    if response.errors:
        error_msg = "Order failed with errors:\n" + "\n".join(str(error) for error in response.errors)
        mcp.send_log_message(level="error", data=error_msg)
        return error_msg

    if dry_run:
        msg = "Dry run successful"
        if response.warnings:
            msg += "\nWarnings:\n" + "\n".join(str(w) for w in response.warnings)
        mcp.send_log_message(level="info", data=msg)
        return msg

    # Monitor and adjust order
    current_order = response.order
    for attempt in range(max_attempts):
        await asyncio.sleep(check_interval)

        orders = account.get_live_orders(session)
        order = next((o for o in orders if o.id == current_order.id), None)

        if not order:
            error_msg = "Order not found during monitoring"
            mcp.send_log_message(level="error", data=error_msg)
            return error_msg

        if order.status == OrderStatus.FILLED:
            success_msg = "Order filled successfully"
            mcp.send_log_message(level="info", data=success_msg)
            return success_msg

        if order.status not in (OrderStatus.LIVE, OrderStatus.RECEIVED):
            error_msg = f"Order in unexpected status: {order.status}"
            mcp.send_log_message(level="error", data=error_msg)
            return error_msg

        # Adjust price
        price_delta = 0.01 if action == "Buy to Open" else -0.01
        new_price = float(order.price) + price_delta
        mcp.send_log_message(
            level="info", 
            data=f"Adjusting order price from ${float(order.price):.2f} to ${new_price:.2f} (attempt {attempt + 1}/{max_attempts})"
        )

        # Replace order with new price
        new_order = NewOrder(
            time_in_force=OrderTimeInForce.DAY,
            order_type=OrderType.LIMIT,
            legs=[leg],
            price=Decimal(str(new_price)) * (-1 if action == "Buy to Open" else 1)
        )

        response = account.replace_order(session, order.id, new_order)
        if response.errors:
            error_msg = f"Failed to adjust order: {response.errors}"
            mcp.send_log_message(level="error", data=error_msg)
            return error_msg

        current_order = response.order

    final_msg = f"Order not filled after {max_attempts} price adjustments"
    mcp.send_log_message(level="warning", data=final_msg)
    return final_msg