import logging
from typing import Literal
from decimal import Decimal
import asyncio

from tastytrade.order import (
    NewOrder,
    OrderAction,
    OrderStatus,
    OrderTimeInForce,
    OrderType,
    Leg
)
from tastytrade.instruments import Option, Equity

from .state import account_state
from .prices import get_prices

# Maximum percentage of net liquidating value for any single position
MAX_POSITION_SIZE_PCT = 0.40  # 40%

# Add logger configuration at the top of the file after imports
logger = logging.getLogger(__name__)

async def place_trade(
    instrument: Option | Equity,
    quantity: int,
    action: Literal["Buy to Open", "Sell to Close"],
    dry_run: bool = False
) -> str:
    """Place a trade for the given instrument"""
    try:
        # Get current price
        try:
            result = await get_prices(instrument.symbol)
            if isinstance(result, str):
                return result
            bid, ask = result
            price = float(ask if action == "Buy to Open" else bid)
        except Exception as e:
            error_msg = f"Failed to get price for {instrument.symbol}: {str(e)}"
            logger.error(error_msg)
            return error_msg

        if action == "Buy to Open":
            multiplier = instrument.multiplier if hasattr(instrument, 'multiplier') else 1
            balances = await account_state.get_balances()
            order_value = Decimal(str(price)) * Decimal(str(quantity)) * Decimal(str(multiplier))

            # Use the appropriate buying power based on instrument type
            buying_power = (
                balances.derivative_buying_power
                if isinstance(instrument, Option)
                else balances.equity_buying_power
            )

            max_value = min(
                buying_power,
                balances.net_liquidating_value * Decimal(str(MAX_POSITION_SIZE_PCT))
            )

            logger.info(
                f"Order value: ${order_value:,.2f}, Max allowed: ${max_value:,.2f}, "
                f"Using {'derivative' if isinstance(instrument, Option) else 'equity'} buying power"
            )

            if order_value > max_value:
                original_quantity = quantity
                quantity = int(max_value / (Decimal(str(price)) * Decimal(str(multiplier))))
                logger.warning(
                    f"Reduced order quantity from {original_quantity} to {quantity} due to position limits"
                )
                if quantity <= 0:
                    error_msg = "Order rejected: Exceeds available funds or position size limits"
                    logger.error(error_msg)
                    return error_msg

        else:  # Sell to Close
            positions = await account_state.get_positions()
            position = next((p for p in positions if p.symbol == instrument.symbol), None)
            if not position:
                error_msg = f"No open position found for {instrument.symbol}"
                logger.error(error_msg)
                return f"Error: No open position found for {instrument.symbol}"

            orders = account_state.get_live_orders()
            pending_sell_quantity = sum(
                sum(leg.quantity for leg in order.legs)
                for order in orders
                if (order.status in (OrderStatus.LIVE, OrderStatus.RECEIVED) and
                    any(leg.symbol == instrument.symbol and
                        leg.action == OrderAction.SELL_TO_CLOSE
                        for leg in order.legs))
            )

            available_quantity = position.quantity - pending_sell_quantity
            logger.info(
                f"Position: {position.quantity}, Pending sells: {pending_sell_quantity}, Available: {available_quantity}"
            )

            if available_quantity <= 0:
                error_msg = (f"Cannot place order - entire position of {position.quantity} "
                             f"already has pending sell orders")
                logger.error(error_msg)
                return f"Error: {error_msg}"

            if quantity > available_quantity:
                logger.warning(
                    f"Reducing sell quantity from {quantity} to {available_quantity} (maximum available)"
                )
                quantity = available_quantity

            if quantity <= 0:
                error_msg = f"Position quantity ({available_quantity}) insufficient for requested sale"
                logger.error(error_msg)
                return f"Error: {error_msg}"

        order_action = OrderAction.BUY_TO_OPEN if action == "Buy to Open" else OrderAction.SELL_TO_CLOSE
        leg: Leg = instrument.build_leg(quantity, order_action)

        logger.info(
            f"Placing initial order: {action} {quantity} {instrument.symbol} @ ${price:.2f}"
        )

        initial_order = NewOrder(
            time_in_force=OrderTimeInForce.DAY,
            order_type=OrderType.LIMIT,
            legs=[leg],
            price=Decimal(str(price)) * (-1 if action == "Buy to Open" else 1)
        )

        response = account_state.place_order(initial_order, dry_run=dry_run)
        if response.errors:
            error_msg = "Order failed with errors:\n" + "\n".join(str(error) for error in response.errors)
            logger.error(error_msg)
            return error_msg

        if dry_run:
            msg = "Dry run successful"
            if response.warnings:
                msg += "\nWarnings:\n" + "\n".join(str(w) for w in response.warnings)
            logger.info(msg)
            return msg

        current_order = response.order
        for attempt in range(20):
            await asyncio.sleep(15.0)

            orders = account_state.get_live_orders()
            order = next((o for o in orders if o.id == current_order.id), None)

            if not order:
                error_msg = "Order not found during monitoring"
                logger.error(error_msg)
                return error_msg

            if order.status == OrderStatus.FILLED:
                success_msg = "Order filled successfully"
                logger.info(success_msg)
                account_state.invalidate_positions()
                account_state.invalidate_balances()
                return success_msg

            if order.status not in (OrderStatus.LIVE, OrderStatus.RECEIVED):
                error_msg = f"Order in unexpected status: {order.status}"
                logger.error(error_msg)
                return error_msg

            price_delta = 0.01 if action == "Buy to Open" else -0.01
            new_price = float(order.price) + price_delta
            logger.info(
                f"Adjusting order price from ${float(order.price):.2f} to ${new_price:.2f} (attempt {attempt + 1}/20)"
            )

            new_order = NewOrder(
                time_in_force=OrderTimeInForce.DAY,
                order_type=OrderType.LIMIT,
                legs=[leg],
                price=Decimal(str(new_price)) * (-1 if action == "Buy to Open" else 1)
            )

            response = account_state.replace_order(order.id, new_order)
            if response.errors:
                error_msg = f"Failed to adjust order: {response.errors}"
                logger.error(error_msg)
                return error_msg

            current_order = response.order

        final_msg = "Order not filled after 20 price adjustments"
        logger.warning(final_msg)
        return final_msg

    except Exception as e:
        logger.exception("Error placing trade")
        return f"Error placing trade: {str(e)}"

async def test_place_trade():
    """Test the place_trade function with a dry run"""
    from .instrument import create_instrument

    # Create a test equity instrument - add await here
    test_stock = await create_instrument("AAPL")
    if not test_stock:
        print("Failed to create test instrument")
        return

    # Test a buy order
    print("\nTesting Buy to Open:")
    result = await place_trade(
        instrument=test_stock,
        quantity=10,
        action="Buy to Open",
        dry_run=True
    )
    print(f"Result: {result}")

    # Test a sell order
    print("\nTesting Sell to Close:")
    result = await place_trade(
        instrument=test_stock,
        quantity=5,
        action="Sell to Close",
        dry_run=True
    )
    print(f"Result: {result}")

if __name__ == "__main__":
    asyncio.run(test_place_trade())
