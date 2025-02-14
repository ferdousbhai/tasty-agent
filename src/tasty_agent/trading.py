from datetime import datetime
from typing import Literal
from decimal import Decimal
import asyncio
import logging

from tastytrade.instruments import Option, Equity
from tastytrade.order import (
    NewOrder,
    OrderAction,
    OrderStatus,
    OrderTimeInForce,
    OrderType,
    Leg
)
from .common import session, account, mcp
from .marketdata import MarketDataService, PositionService, get_instrument_for_symbol

logger = logging.getLogger(__name__)

# Maximum percentage of net liquidating value for any single position
MAX_POSITION_SIZE_PCT = 0.40  # 40%

class TradingService:
    """Service for placing and managing trades"""
    def __init__(self, session, account):
        self.session = session
        self.account = account
        self.market_data = MarketDataService(session, account)
        self.positions = PositionService(session, account)

    def log_message(self, level: str, data: str) -> None:
        """Helper method to use global mcp"""
        mcp.send_log_message(level=level, data=data)

    async def validate_buy_order(
        self,
        instrument: Option | Equity,
        quantity: int,
        price: Decimal
    ) -> tuple[bool, int]:
        """Validate buy order against position limits and buying power."""
        multiplier = instrument.multiplier if hasattr(instrument, 'multiplier') else 1
        balances = await self.account.a_get_balances(self.session)

        order_value = price * Decimal(str(quantity)) * Decimal(str(multiplier))
        max_value = min(
            balances.buying_power,
            balances.net_liquidating_value * Decimal(str(MAX_POSITION_SIZE_PCT))
        )

        if order_value > max_value:
            new_quantity = int(max_value / (price * Decimal(str(multiplier))))
            return False, new_quantity
        return True, quantity


async def place_trade(
    symbol: str,
    quantity: int,
    action: Literal["Buy to Open", "Sell to Close"],
    dry_run: bool = True,
    check_interval: float = 15.0,
    max_attempts: int = 20,
    expiration_date: datetime | None = None,
    option_type: Literal["C", "P"] | None = None,
    strike: float | None = None,
) -> str:
    """Place a trade order (buy to open or sell to close).

    Args:
        session: TastyTrade session
        account: TastyTrade account
        symbol: Underlying symbol (e.g., "SPY", "AAPL")
        quantity: Number of shares/contracts
        action: Trade action - either "Buy to Open" or "Sell to Close"
        mcp: MCP instance for logging to Claude Desktop
        dry_run: If True, simulates the order
        check_interval: Seconds to wait between price adjustment attempts
        max_attempts: Maximum number of price adjustments to attempt
        expiration_date: Optional expiration date for options
        option_type: Optional option type ("C" for call, "P" for put)
        strike: Optional strike price
    """
    trading_svc = TradingService(session, account)

    try:
        # Get instrument
        instrument = await get_instrument_for_symbol(
            symbol=symbol,
            expiration_date=expiration_date,
            option_type=option_type,
            strike=strike
        )
        if not instrument:
            raise ValueError("Failed to get instrument details")

        # Get current price
        try:
            bid, ask = await trading_svc.market_data.get_prices(
                symbol=symbol,
                expiration_date=expiration_date,
                option_type=option_type,
                strike=strike
            )
            bid, ask = Decimal(bid), Decimal(ask)
            price = float(ask if action == "Buy to Open" else bid)
        except Exception as e:
            error_msg = f"Failed to get price for {instrument.symbol}: {str(e)}"
            trading_svc.log_message(level="error", data=error_msg)
            return error_msg

        if action == "Buy to Open":
            multiplier = instrument.multiplier if hasattr(instrument, 'multiplier') else 1

            balances = await account.a_get_balances(session)
            order_value = Decimal(str(price)) * Decimal(str(quantity)) * Decimal(str(multiplier))
            max_value = min(
                balances.buying_power,
                balances.net_liquidating_value * Decimal(str(MAX_POSITION_SIZE_PCT))
            )

            trading_svc.log_message(
                level="info",
                data=f"Order value: ${order_value:,.2f}, Max allowed: ${max_value:,.2f}"
            )

            if order_value > max_value:
                original_quantity = quantity
                quantity = int(max_value / (Decimal(str(price)) * Decimal(str(multiplier))))
                trading_svc.log_message(
                    level="warning",
                    data=f"Reduced order quantity from {original_quantity} to {quantity} due to position limits"
                )
                if quantity <= 0:
                    trading_svc.log_message(
                        level="error",
                        data="Order rejected: Exceeds available funds or position size limits"
                    )
                    return "Order rejected: Exceeds available funds or position size limits"

        else:  # Sell to Close
            positions = await account.a_get_positions(session)
            position = next((p for p in positions if p.symbol == instrument.symbol), None)
            if not position:
                trading_svc.log_message(
                    level="error",
                    data=f"No open position found for {instrument.symbol}"
                )
                return f"Error: No open position found for {instrument.symbol}"

            orders = account.get_live_orders(session)
            pending_sell_quantity = sum(
                sum(leg.quantity for leg in order.legs)
                for order in orders
                if (order.status in (OrderStatus.LIVE, OrderStatus.RECEIVED) and
                    any(leg.symbol == instrument.symbol and
                        leg.action == OrderAction.SELL_TO_CLOSE
                        for leg in order.legs))
            )

            available_quantity = position.quantity - pending_sell_quantity
            trading_svc.log_message(
                level="info",
                data=f"Position: {position.quantity}, Pending sells: {pending_sell_quantity}, Available: {available_quantity}"
            )

            if available_quantity <= 0:
                error_msg = (f"Cannot place order - entire position of {position.quantity} "
                            f"already has pending sell orders")
                trading_svc.log_message(level="error", data=error_msg)
                return f"Error: {error_msg}"

            if quantity > available_quantity:
                trading_svc.log_message(
                    level="warning",
                    data=f"Reducing sell quantity from {quantity} to {available_quantity} (maximum available)"
                )
                quantity = available_quantity

            if quantity <= 0:
                error_msg = f"Position quantity ({available_quantity}) insufficient for requested sale"
                trading_svc.log_message(level="error", data=error_msg)
                return f"Error: {error_msg}"

        order_action = OrderAction.BUY_TO_OPEN if action == "Buy to Open" else OrderAction.SELL_TO_CLOSE
        leg: Leg = instrument.build_leg(quantity, order_action)

        trading_svc.log_message(
            level="info",
            data=f"Placing initial order: {action} {quantity} {instrument.symbol} @ ${price:.2f}"
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
            trading_svc.log_message(level="error", data=error_msg)
            return error_msg

        if dry_run:
            msg = "Dry run successful"
            if response.warnings:
                msg += "\nWarnings:\n" + "\n".join(str(w) for w in response.warnings)
            trading_svc.log_message(level="info", data=msg)
            return msg

        current_order = response.order
        for attempt in range(max_attempts):
            await asyncio.sleep(check_interval)

            orders = account.get_live_orders(session)
            order = next((o for o in orders if o.id == current_order.id), None)

            if not order:
                error_msg = "Order not found during monitoring"
                trading_svc.log_message(level="error", data=error_msg)
                return error_msg

            if order.status == OrderStatus.FILLED:
                success_msg = "Order filled successfully"
                trading_svc.log_message(level="info", data=success_msg)
                return success_msg

            if order.status not in (OrderStatus.LIVE, OrderStatus.RECEIVED):
                error_msg = f"Order in unexpected status: {order.status}"
                trading_svc.log_message(level="error", data=error_msg)
                return error_msg

            price_delta = 0.01 if action == "Buy to Open" else -0.01
            new_price = float(order.price) + price_delta
            trading_svc.log_message(
                level="info",
                data=f"Adjusting order price from ${float(order.price):.2f} to ${new_price:.2f} (attempt {attempt + 1}/{max_attempts})"
            )

            new_order = NewOrder(
                time_in_force=OrderTimeInForce.DAY,
                order_type=OrderType.LIMIT,
                legs=[leg],
                price=Decimal(str(new_price)) * (-1 if action == "Buy to Open" else 1)
            )

            response = account.replace_order(session, order.id, new_order)
            if response.errors:
                error_msg = f"Failed to adjust order: {response.errors}"
                trading_svc.log_message(level="error", data=error_msg)
                return error_msg

            current_order = response.order

        final_msg = f"Order not filled after {max_attempts} price adjustments"
        trading_svc.log_message(level="warning", data=final_msg)
        return final_msg

    except Exception as e:
        return f"Error placing trade: {str(e)}"
