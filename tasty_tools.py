import asyncio
from decimal import Decimal
from dotenv import load_dotenv
import os
import logging

import modal
import tastytrade

# check Option.occ_to_streamer_symbol, Option.get_option, Option.get_options

# modal functions
get_options = modal.Function.lookup("get-options", "get_options")

logging.basicConfig(level=logging.INFO)


load_dotenv()


# try to get cached session
try:
    session = ...
except Exception:
    ...

try:
    session = tastytrade.Session(
        os.getenv("TASTYTRADE_USER"),
        os.getenv("TASTYTRADE_PASSWORD"),
    )
except Exception:
    logging.error("Failed to create session. Check your credentials.")
    exit()

try:
    account = tastytrade.Account.get_account(session, os.getenv("TASTYTRADE_ACCOUNT"))
except Exception:
    account = tastytrade.Account.get_accounts(session)[0]


def get_balances():
    balances = account.get_balances(session)
    return {
        attr: getattr(balances, attr)
        for attr in [
            "cash_balance",
            "net_liquidating_value",
            "equity_buying_power",
            "derivative_buying_power",
            "margin_equity",
            "maintenance_excess",
        ]
    }


def get_positions():
    return [
        {
            "symbol": p.symbol,
            "quantity": p.quantity,
            "quantity_direction": p.quantity_direction,
            "instrument_type": p.instrument_type.value,
        }
        for p in account.get_positions(session)
    ]


def get_market_metrics(symbols: list[str]):
    metrics = tastytrade.metrics.get_market_metrics(session, symbols)
    return [
        {
            "symbol": m.symbol,
            "implied_volatility_index": m.implied_volatility_index,
            "implied_volatility_index_rank": m.implied_volatility_index_rank,
            "implied_volatility_percentile": m.implied_volatility_percentile,
            "liquidity_rating": m.liquidity_rating,
            "liquidity_value": m.liquidity_value,
            "liquidity_rank": m.liquidity_rank,
            "lendability": m.lendability,
            "borrow_rate": m.borrow_rate,
            "expected_report_date": m.earnings.expected_report_date
            if m.earnings
            else None,
        }
        for m in metrics
    ]


async def is_market_open() -> bool:
    try:
        async with tastytrade.DXLinkStreamer(session) as streamer:
            await streamer.subscribe(tastytrade.dxfeed.event.EventType.PROFILE, ["SPY"])
            profile = await asyncio.wait_for(
                streamer.get_event(tastytrade.dxfeed.event.EventType.PROFILE),
                timeout=5.0,
            )
            return profile.tradingStatus == "ACTIVE"
    except (asyncio.TimeoutError, Exception) as e:
        logging.error(f"Error checking market status: {e}")
        return False


async def get_price(streamer_symbol: str) -> Decimal:
    async with tastytrade.DXLinkStreamer(session) as streamer:
        await streamer.subscribe(
            tastytrade.dxfeed.event.EventType.QUOTE, [streamer_symbol]
        )
        quote = await streamer.get_event(tastytrade.dxfeed.event.EventType.QUOTE)
        return Decimal(round((quote.bidPrice + quote.askPrice) / 2 * 20) / 20)


async def buy_to_open(
    option_streamer_symbol: str,
    budget: Decimal | None = None,
    price: Decimal | None = None,
    quantity: int | None = None,
):
    option = tastytrade.instruments.Option.get_option(
        session,
        tastytrade.instruments.Option.streamer_symbol_to_occ(option_streamer_symbol),
    )
    price = price or await get_price(option_streamer_symbol)
    quantity = quantity or (
        Decimal(budget) // price // option.shares_per_contract if budget else None
    )

    if not quantity:
        logging.info("buy_quantity: 0")
        return

    leg = option.build_leg(quantity, tastytrade.order.OrderAction.BUY_TO_OPEN)
    order = tastytrade.order.NewOrder(
        time_in_force=tastytrade.order.OrderTimeInForce.DAY,
        order_type=tastytrade.order.OrderType.LIMIT,
        legs=[leg],
        price=price,
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


if __name__ == "__main__":
    ...
