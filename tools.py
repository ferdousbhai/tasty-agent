import logging
import asyncio
from typing import Literal
from decimal import Decimal
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import tastytrade
import matplotlib.pyplot as plt

from auth import session, account


logging.basicConfig(level=logging.INFO)


def get_balances():
    return account.get_balances(session)

def get_positions():
    return account.get_positions(session)

def get_transactions(start_date=None):
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

def plot_nlv_history(
    time_back: Literal['1d', '1m', '3m', '6m', '1y', 'all'] = '1y'
) -> None:
    """Plots account net liquidating value history over time and displays it to the user.

    Args:
        time_back: Time period to plot. Options: '1d', '1m', '3m', '6m', '1y', 'all'
    """
    # Get historical data
    history = account.get_net_liquidating_value_history(session, time_back=time_back)

    # Create the plot
    plt.figure(figsize=(10, 6))
    plt.plot([n.time for n in history], [n.close for n in history], 'b-')

    # Customize the plot
    plt.title(f'Portfolio Value History (Past {time_back})')
    plt.xlabel('Date')
    plt.ylabel('Portfolio Value ($)')
    plt.grid(True)

    # Display the plot
    plt.show()

async def get_market_metrics(symbols: list[str]):
    from tastytrade import metrics
    return await metrics.a_get_market_metrics(session, symbols)

async def is_market_open() -> bool:
    """Check if the US stock market is currently open for regular trading hours.

    Returns:
        bool: True if market is open for regular trading (9:30 AM - 4:00 PM ET), False otherwise
    """
    from tastytrade.dxfeed import Quote

    # First check if we're in regular trading hours (9:30 AM - 4:00 PM ET)
    current_time = datetime.now(ZoneInfo("America/New_York"))
    market_open = current_time.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = current_time.replace(hour=16, minute=0, second=0, microsecond=0)

    if not (market_open <= current_time <= market_close):
        return False

    # If we're in market hours, verify we have active trading
    try:
        async with tastytrade.DXLinkStreamer(session) as streamer:
            await streamer.subscribe(Quote, ["SPY"])
            quote = await streamer.get_event(Quote)
            return bool(quote.bid_size and quote.ask_size)

    except (asyncio.TimeoutError, Exception) as e:
        logging.error(f"Error checking market status: {e}")
        return False

async def get_price(streamer_symbol: str) -> float:
    """Get the current price for a symbol.

    Returns the mid-point price as a float rounded to 2 decimal places.
    """
    from tastytrade.dxfeed import Quote

    async with tastytrade.DXLinkStreamer(session) as streamer:
        await streamer.subscribe(Quote, [streamer_symbol])
        quote = await streamer.get_event(Quote)
        # Convert to Decimal for precise calculation, then back to float for return
        price_decimal = Decimal(str((quote.bid_price + quote.ask_price) / 2))
        return float(round(price_decimal * 20) / 20)

async def buy_to_open(
    option_streamer_symbol: str,
    budget: float | None = None,
    price: float | None = None,
    quantity: int | None = None,
):
    """Buy to open an option position.

    Args:
        budget: Maximum amount to spend (as float)
        price: Limit price (as float)
        quantity: Number of contracts
    """
    option = tastytrade.instruments.Option.get_option(
        session,
        tastytrade.instruments.Option.streamer_symbol_to_occ(option_streamer_symbol),
    )

    # Convert price to Decimal if provided, otherwise get current price
    decimal_price = Decimal(str(price)) if price is not None else await get_price(option_streamer_symbol)

    # Calculate quantity if budget provided
    if quantity is None and budget is not None:
        quantity = int(Decimal(str(budget)) // decimal_price // option.shares_per_contract)

    if not quantity:
        logging.info("buy_quantity: 0")
        return

    leg = option.build_leg(quantity, tastytrade.order.OrderAction.BUY_TO_OPEN)
    order = tastytrade.order.NewOrder(
        time_in_force=tastytrade.order.OrderTimeInForce.DAY,
        order_type=tastytrade.order.OrderType.LIMIT,
        legs=[leg],
        price=decimal_price,
        price_effect=tastytrade.order.PriceEffect.DEBIT,
    )
    response = account.place_order(session, order, dry_run=False)
    logging.info(f"response: {response}")
    return {...}  # Return appropriate response data


async def sell_to_close(position_occ_symbol: str, quantity: int):
    option = tastytrade.instruments.Option.get_option(session, position_occ_symbol)
    price = await get_price(option.streamer_symbol)

    positions = get_positions()
    position = next((p for p in positions if p["symbol"] == position_occ_symbol), None)
    if not position:
        logging.error(f"Position not found: {position_occ_symbol}")
        return
    quantity = min(quantity, position["quantity"])

    leg = option.build_leg(quantity, tastytrade.order.OrderAction.SELL_TO_CLOSE)
    order = tastytrade.order.NewOrder(
        time_in_force=tastytrade.order.OrderTimeInForce.DAY,
        order_type=tastytrade.order.OrderType.LIMIT,
        legs=[leg],
        price=price,
        price_effect=tastytrade.order.PriceEffect.CREDIT,
    )
    response = account.place_order(session, order, dry_run=False)
    logging.info(f"response: {response}")
    return response

if __name__ == '__main__':
    price = asyncio.run(get_price("SPY"))
    print(price)
