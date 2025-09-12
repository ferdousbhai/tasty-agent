import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta, datetime, date, timezone
from decimal import Decimal
import os
from typing import Literal, AsyncIterator, Any

from aiolimiter import AsyncLimiter
import humanize
from mcp.server.fastmcp import FastMCP, Context
from tastytrade import OAuthSession, Account
from tastytrade.dxfeed import Quote, Greeks
from tastytrade.instruments import Equity, Option, a_get_option_chain
from tastytrade.market_sessions import a_get_market_sessions, a_get_market_holidays, ExchangeType, MarketStatus
from tastytrade.metrics import a_get_market_metrics
from tastytrade.order import NewOrder, OrderAction, OrderTimeInForce, OrderType
from tastytrade.search import a_symbol_search
from tastytrade.streamer import DXLinkStreamer
from tastytrade.utils import now_in_new_york
from tastytrade.watchlists import PublicWatchlist, PrivateWatchlist

# Simple cache for option chains
_option_chains = {}

# Rate limiter: 5 requests per second
rate_limiter = AsyncLimiter(5, 1)

@dataclass
class ServerContext:
    session: OAuthSession
    account: Account


def get_context(ctx: Context) -> ServerContext:
    """Extract context from request."""
    return ctx.request_context.lifespan_context

@asynccontextmanager
async def lifespan(_) -> AsyncIterator[ServerContext]:
    """Manages Tastytrade session lifecycle."""

    client_secret = os.getenv("TASTYTRADE_CLIENT_SECRET")
    refresh_token = os.getenv("TASTYTRADE_REFRESH_TOKEN")
    account_id = os.getenv("TASTYTRADE_ACCOUNT_ID")

    if not client_secret or not refresh_token:
        raise ValueError(
            "Missing Tastytrade OAuth credentials. Set TASTYTRADE_CLIENT_SECRET and "
            "TASTYTRADE_REFRESH_TOKEN environment variables."
        )

    session = OAuthSession(client_secret, refresh_token)
    accounts = Account.get(session)

    if account_id:
        account = next((acc for acc in accounts if acc.account_number == account_id), None)
        if not account:
            raise ValueError(f"Account '{account_id}' not found.")
    else:
        account = accounts[0]

    yield ServerContext(
        session=session,
        account=account
    )

mcp_app = FastMCP("TastyTrade", lifespan=lifespan)

@mcp_app.tool()
async def get_balances(ctx: Context) -> dict[str, Any]:
    context = get_context(ctx)
    return {k: v for k, v in (await context.account.a_get_balances(context.session)).model_dump().items() if v is not None and v != 0}


@mcp_app.tool()
async def get_positions(ctx: Context) -> list[dict[str, Any]]:
    context = get_context(ctx)
    return [pos.model_dump() for pos in await context.account.a_get_positions(context.session, include_marks=True)]


async def find_option_instrument(session: OAuthSession, symbol: str, expiration_date: str, option_type: Literal['C', 'P'], strike_price: float) -> Option:
    """Helper function to find an option instrument using the option chain."""
    
    # Cache option chains to reduce API calls
    if symbol not in _option_chains:
        _option_chains[symbol] = await a_get_option_chain(session, symbol)
    chain = _option_chains[symbol]
    target_date = datetime.strptime(expiration_date, "%Y-%m-%d").date()

    if target_date not in chain:
        raise ValueError(f"No options found for expiration date {expiration_date}")

    for option in chain[target_date]:
        if (option.strike_price == strike_price and
            option.option_type.value == option_type.upper()):
            return option

    raise ValueError(f"Option not found: {symbol} {expiration_date} {option_type} {strike_price}")


@mcp_app.tool()
async def get_quote(
    ctx: Context,
    symbol: str,
    option_type: Literal['C', 'P'] | None = None,
    strike_price: float | None = None,
    expiration_date: str | None = None,
    timeout: float = 10.0
) -> dict[str, Any]:
    """
    Get live quote for a stock or option.

    Args:
        symbol: Stock symbol (e.g., 'AAPL', 'TQQQ')
        option_type: 'C' or 'P' (required for options)
        strike_price: Strike price (required for options)
        expiration_date: Expiration date in YYYY-MM-DD format (required for options)
        timeout: Timeout in seconds

    Examples:
        Stock: get_quote("AAPL")
        Option: get_quote("TQQQ", "C", 100.0, "2026-01-16")
    """
    context = get_context(ctx)

    # For options, find the option using helper function
    if option_type is not None:
        if strike_price is None or expiration_date is None:
            raise ValueError("strike_price and expiration_date are required for option quotes")

        streamer_symbol = (await find_option_instrument(context.session, symbol, expiration_date, option_type, strike_price)).streamer_symbol
    else:
        streamer_symbol = symbol

    try:
        async with DXLinkStreamer(context.session) as streamer:
            await streamer.subscribe(Quote, [streamer_symbol])
            return (await asyncio.wait_for(streamer.get_event(Quote), timeout=timeout)).model_dump()
    except asyncio.TimeoutError:
        raise ValueError(f"Timeout getting quote for {streamer_symbol} after {timeout}s")
    except Exception as e:
        raise ValueError(f"Error getting quote: {str(e)}")


@mcp_app.tool()
async def get_greeks(
    ctx: Context,
    symbol: str,
    option_type: Literal['C', 'P'],
    strike_price: float,
    expiration_date: str,
    timeout: float = 10.0
) -> dict[str, Any]:
    """
    Get Greeks (delta, gamma, theta, vega, rho) for an option.

    Args:
        symbol: Stock symbol (e.g., 'AAPL', 'TQQQ')
        option_type: 'C' for call or 'P' for put
        strike_price: Strike price of the option
        expiration_date: Expiration date in YYYY-MM-DD format
        timeout: Timeout in seconds

    Examples:
        get_greeks("TQQQ", "C", 100.0, "2026-01-16")
        get_greeks("AAPL", "P", 150.0, "2024-12-20")
    """
    context = get_context(ctx)

    # Find the option using helper function
    option = await find_option_instrument(context.session, symbol, expiration_date, option_type, strike_price)
    
    try:
        async with DXLinkStreamer(context.session) as streamer:
            await streamer.subscribe(Greeks, [option.streamer_symbol])
            return (await asyncio.wait_for(streamer.get_event(Greeks), timeout=timeout)).model_dump()
    except asyncio.TimeoutError:
        raise ValueError(f"Timeout getting Greeks for {option.streamer_symbol} after {timeout}s")
    except Exception as e:
        raise ValueError(f"Error getting Greeks: {str(e)}")


@mcp_app.tool()
async def get_net_liquidating_value_history(
    ctx: Context,
    time_back: Literal['1d', '1m', '3m', '6m', '1y', 'all'] = '1y'
) -> list[dict[str, Any]]:
    context = get_context(ctx)
    return [h.model_dump() for h in await context.account.a_get_net_liquidating_value_history(context.session, time_back=time_back)]


@mcp_app.tool()
async def get_history(
    ctx: Context,
    start_date: str | None = None
) -> list[dict[str, Any]]:
    """start_date format: YYYY-MM-DD."""
    context = get_context(ctx)
    return [txn.model_dump() for txn in await context.account.a_get_history(context.session, start_date=date.today() - timedelta(days=90) if start_date is None else datetime.strptime(start_date, "%Y-%m-%d").date())]


@mcp_app.tool()
async def get_market_metrics(ctx: Context, symbols: list[str]) -> list[dict[str, Any]]:
    """
    Get market metrics including volatility (IV/HV), risk (beta, correlation),
    valuation (P/E, market cap), liquidity, dividends, earnings, and options data.
    
    Note extreme IV rank/percentile (0-1): low = cheap options (buy opportunity), high = expensive options (close positions).
    """
    context = get_context(ctx)
    return [m.model_dump() for m in await a_get_market_metrics(context.session, symbols)]


@mcp_app.tool()
async def market_status(ctx: Context, exchanges: list[Literal['NYSE', 'CME', 'CFE', 'Smalls']] = ['NYSE']) -> list[dict[str, Any]]:
    """
    Get market status for each exchange including current open/closed state,
    next opening times, and holiday information.
    """
    context = get_context(ctx)
    market_sessions = await a_get_market_sessions(context.session, [ExchangeType(exchange) for exchange in exchanges])

    if not market_sessions:
        raise ValueError("No market sessions found")

    current_time = datetime.now(timezone.utc)
    calendar = await a_get_market_holidays(context.session)
    is_holiday = current_time.date() in calendar.holidays
    is_half_day = current_time.date() in calendar.half_days

    results = []
    for market_session in market_sessions:
        if market_session.status == MarketStatus.OPEN:
            result = {
                "exchange": market_session.instrument_collection,
                "status": market_session.status.value,
                "close_at": market_session.close_at.isoformat() if market_session.close_at else None,
            }
        else:
            open_at = (
                market_session.open_at if market_session.status == MarketStatus.PRE_MARKET and market_session.open_at else
                market_session.open_at if market_session.status == MarketStatus.CLOSED and market_session.open_at and current_time < market_session.open_at else
                market_session.next_session.open_at if market_session.status == MarketStatus.CLOSED and market_session.close_at and current_time > market_session.close_at and market_session.next_session and market_session.next_session.open_at else
                market_session.next_session.open_at if market_session.status == MarketStatus.EXTENDED and market_session.next_session and market_session.next_session.open_at else
                None
            )

            result = {
                "exchange": market_session.instrument_collection,
                "status": market_session.status.value,
                **({"next_open": open_at.isoformat(), "time_until_open": humanize.naturaldelta(open_at - current_time)} if open_at else {}),
                **({"is_holiday": True} if is_holiday else {}),
                **({"is_half_day": True} if is_half_day else {})
            }
        results.append(result)
    return results


@mcp_app.tool()
async def search_symbols(ctx: Context, symbol: str) -> list[dict[str, Any]]:
    """Search for symbols similar to the given search phrase."""
    context = get_context(ctx)
    return [result.model_dump() for result in await a_symbol_search(context.session, symbol)]


@mcp_app.tool()
async def get_live_orders(ctx: Context) -> list[dict[str, Any]]:
    context = get_context(ctx)
    return [order.model_dump() for order in await context.account.a_get_live_orders(context.session)]


@mcp_app.tool()
async def place_order(
    ctx: Context,
    symbol: str,
    order_type: Literal['C', 'P', 'Stock'],
    action: Literal['Buy', 'Sell'],
    quantity: int,
    price: float | None = None,
    strike_price: float | None = None,
    expiration_date: str | None = None,
    time_in_force: Literal['Day', 'GTC', 'IOC'] = 'Day',
    dry_run: bool = False,
    override_price_protection: bool = False
) -> dict[str, Any]:
    """
    Place an options or equity order.
    
    Automatically calculates optimal pricing and validates against market conditions:
    - If price not provided (default): uses mid-price between bid/ask, rounded to nearest 5 cents
    - Price protection: buy orders capped at ask, sell orders floored at bid (unless overridden)
    After placing order, check order status and modify price if needed until filled.

    Args:
        symbol: Stock symbol (e.g., 'TQQQ', 'AAPL')
        order_type: 'C', 'P', or 'Stock'
        action: 'Buy' or 'Sell'
        quantity: Number of contracts or shares. 1 option contract = 100 shares of the underlying stock.
        price: Limit price. If None, uses mid-price between bid/ask rounded to nearest 5 cents.
        strike_price: Strike price (required for options)
        expiration_date: Expiration date in YYYY-MM-DD format (required for options)
        time_in_force: 'Day', 'GTC', or 'IOC'
        dry_run: If True, validates order without placing it
        override_price_protection: If True, bypasses bid/ask price validation

    Examples:
        Auto-price: place_order("AAPL", "Stock", "Buy", 100)
        Set price: place_order("AAPL", "Stock", "Buy", 100, 150.00)
        Options: place_order("TQQQ", "C", "Buy", 17, strike_price=100.0, expiration_date="2026-01-16")
        Override: place_order("NVDA", "Stock", "Buy", 100, 155.00, override_price_protection=True)
    """
    async with rate_limiter:
        context = get_context(ctx)

        # Determine streamer symbol for quote fetching
        if order_type in ['C', 'P']:
            if not strike_price or not expiration_date:
                raise ValueError(f"strike_price and expiration_date are required for {order_type} orders")
            
            option_instrument = await find_option_instrument(context.session, symbol, expiration_date, order_type, strike_price)
            streamer_symbol = option_instrument.streamer_symbol
            instrument = option_instrument
            order_action = OrderAction.BUY_TO_OPEN if action == 'Buy' else OrderAction.SELL_TO_CLOSE
        else:
            streamer_symbol = symbol
            instrument = await Equity.a_get(context.session, symbol)
            order_action = OrderAction.BUY if action == 'Buy' else OrderAction.SELL

        # Fetch current quote for price calculation and/or validation
        try:
            async with DXLinkStreamer(context.session) as streamer:
                await streamer.subscribe(Quote, [streamer_symbol])
                quote = await asyncio.wait_for(streamer.get_event(Quote), timeout=10.0)
                
                # Calculate price if not provided
                if price is None:
                    if quote.bid_price and quote.ask_price:
                        mid_price = (quote.bid_price + quote.ask_price) / 2
                        # Round to nearest 5 cents
                        price = round(mid_price * 20) / 20
                        await ctx.info(f"💰 Using mid-price: ${price:.2f} (bid: ${quote.bid_price}, ask: ${quote.ask_price})")
                    else:
                        raise ValueError(f"Could not calculate mid-price: missing bid ({quote.bid_price}) or ask ({quote.ask_price}) price")
                
                # Apply price protection unless overridden
                if not override_price_protection:
                    if action == 'Buy' and quote.ask_price and price > quote.ask_price:
                        await ctx.info(f"⚠️  Reducing buy price from ${price:.2f} to ask price ${quote.ask_price:.2f} to prevent overpaying")
                        price = float(quote.ask_price)
                    elif action == 'Sell' and quote.bid_price and price < quote.bid_price:
                        await ctx.info(f"⚠️  Increasing sell price from ${price:.2f} to bid price ${quote.bid_price:.2f} to prevent underselling")
                        price = float(quote.bid_price)
                elif price is not None:
                    await ctx.info(f"⚠️  Price protection overridden. Using price ${price:.2f} without validation.")
                        
        except Exception as e:
            if price is None:
                raise ValueError(f"Could not fetch quote for price calculation: {str(e)}. Please provide a price.")
            await ctx.info(f"⚠️  Could not fetch quote for price validation: {str(e)}. Proceeding with provided price ${price:.2f}.")

        order = NewOrder(
            time_in_force=OrderTimeInForce(time_in_force),
            order_type=OrderType.LIMIT,
            legs=[instrument.build_leg(Decimal(str(quantity)), order_action)],
            price=Decimal(str(-abs(price) if action == 'Buy' else abs(price)))
        )

        return (await context.account.a_place_order(context.session, order, dry_run=dry_run)).model_dump()


@mcp_app.tool()
async def delete_order(ctx: Context, order_id: str) -> dict[str, Any]:
    context = get_context(ctx)
    await context.account.a_delete_order(context.session, int(order_id))
    return {"success": True, "order_id": order_id}


@mcp_app.tool()
async def get_watchlists(
    ctx: Context,
    watchlist_type: Literal['public', 'private'] = 'private',
    name: str | None = None
) -> list[dict[str, Any]] | dict[str, Any]:
    """
    Get watchlists for market insights and tracking.
    
    No name = list watchlist names. With name = get symbols in that watchlist. For private, default to "main".
    """
    context = get_context(ctx)

    watchlist_class = PublicWatchlist if watchlist_type == 'public' else PrivateWatchlist
    
    if name:
        return (await watchlist_class.a_get(context.session, name)).model_dump()
    else:
        watchlists = await watchlist_class.a_get(context.session)
        return [w.model_dump() for w in watchlists]


@mcp_app.tool()
async def manage_private_watchlist(
    ctx: Context,
    action: Literal["add", "remove"],
    symbol: str,
    instrument_type: Literal["Equity", "Equity Option", "Future", "Future Option", "Cryptocurrency", "Warrant"],
    name: str = "main"
) -> None:
    """Add or remove symbols from a private watchlist."""
    context = get_context(ctx)

    if action == "add":
        try:
            watchlist = await PrivateWatchlist.a_get(context.session, name)
            watchlist.add_symbol(symbol, instrument_type)
            await watchlist.a_update(context.session)
            await ctx.info(f"✅ Added {symbol} to watchlist '{name}'")
        except Exception:
            watchlist = PrivateWatchlist(
                name=name,
                group_name="main",
                watchlist_entries=[{"symbol": symbol, "instrument_type": instrument_type}]
            )
            await watchlist.a_upload(context.session)
            await ctx.info(f"✅ Created watchlist '{name}' and added {symbol}")
    else:
        watchlist = await PrivateWatchlist.a_get(context.session, name)
        watchlist.remove_symbol(symbol, instrument_type)
        await watchlist.a_update(context.session)
        await ctx.info(f"✅ Removed {symbol} from watchlist '{name}'")


@mcp_app.tool()
async def delete_private_watchlist(ctx: Context, name: str) -> None:
    context = get_context(ctx)
    await PrivateWatchlist.a_remove(context.session, name)
    await ctx.info(f"✅ Deleted private watchlist '{name}'")


@mcp_app.tool()
async def get_current_time_nyc() -> str:
    return now_in_new_york().isoformat()
