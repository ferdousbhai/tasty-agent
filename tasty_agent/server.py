import asyncio
import logging
import os
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

import humanize
from aiocache import Cache, cached
from aiocache.serializers import PickleSerializer
from aiolimiter import AsyncLimiter
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.prompts.base import AssistantMessage, Message, UserMessage
from pydantic import BaseModel, Field
from tabulate import tabulate
from tastytrade import Account, Session, metrics
from tastytrade.dxfeed import Greeks, Quote, Summary, Trade
from tastytrade.instruments import Equity, Future, Option, get_option_chain
from tastytrade.market_sessions import ExchangeType, MarketStatus, get_market_holidays, get_market_sessions
from tastytrade.order import InstrumentType, NewOrder, OrderAction, OrderTimeInForce, OrderType
from tastytrade.search import symbol_search
from tastytrade.streamer import DXLinkStreamer
from tastytrade.utils import now_in_new_york
from tastytrade.watchlists import PrivateWatchlist, PublicWatchlist

logger = logging.getLogger(__name__)

rate_limiter = AsyncLimiter(2, 1) # 2 requests per second


def _exchanges_for_symbols(streamer_symbols: list[str]) -> set[ExchangeType]:
    """Determine which exchanges to check based on streamer symbols.

    Maps streamer symbol prefixes/suffixes to exchange types:
    - /VX* or :XCBF suffix → CFE (CBOE Futures Exchange)
    - /* (other futures) → CME
    - Everything else → NYSE (covers all US equities in the tastytrade API)
    """
    exchanges: set[ExchangeType] = set()
    for sym in streamer_symbols:
        if sym.startswith("/"):
            if ":XCBF" in sym or sym.startswith("/VX"):
                exchanges.add(ExchangeType.CFE)
            else:
                exchanges.add(ExchangeType.CME)
        else:
            exchanges.add(ExchangeType.NYSE)
    return exchanges


async def _market_status_message(session: Session, exchanges: set[ExchangeType]) -> str | None:
    """Check if relevant markets are closed and return a human-readable message, or None if open."""
    try:
        market_sessions = await get_market_sessions(session, list(exchanges))
    except Exception:
        logger.debug("Failed to fetch market sessions for %s", exchanges, exc_info=True)
        return None

    current_time = datetime.now(UTC)
    closed: list[str] = []
    for ms in market_sessions:
        if ms.status != MarketStatus.OPEN:
            next_open = _get_next_open_time(ms, current_time)
            label = ms.instrument_collection
            if next_open:
                delta = humanize.naturaldelta(next_open - current_time)
                closed.append(f"{label} (opens in {delta})")
            else:
                closed.append(f"{label} (closed)")
    if closed:
        return f"Market is currently closed: {', '.join(closed)}. Live quotes are not available while the market is closed."
    return None


async def _raise_with_market_context(
    session: Session,
    exchanges: set[ExchangeType],
    fallback_error: ValueError
) -> None:
    """Raise a market-closed message if applicable, otherwise raise the fallback error."""
    market_msg = await _market_status_message(session, exchanges)
    if market_msg:
        raise ValueError(market_msg) from fallback_error
    raise fallback_error


async def _stream_events(
    session: Session,
    event_type: type[Quote] | type[Greeks],
    streamer_symbols: list[str],
    timeout: float
) -> list[Any]:
    """Generic streaming helper for Quote/Greeks events."""
    events_by_symbol: dict[str, Any] = {}
    expected = set(streamer_symbols)
    exchanges = _exchanges_for_symbols(streamer_symbols)
    timed_out = False
    try:
        async with DXLinkStreamer(session) as streamer:
            await streamer.subscribe(event_type, streamer_symbols)
            try:
                async with asyncio.timeout(timeout):
                    while len(events_by_symbol) < len(expected):
                        event = await streamer.get_event(event_type)
                        if event.event_symbol in expected:
                            events_by_symbol[event.event_symbol] = event
            except TimeoutError:
                # Don't raise here — we're still inside DXLinkStreamer's TaskGroup.
                # Any exception through the yield point can get wrapped in an
                # ExceptionGroup during background task cleanup.
                timed_out = True
    except ExceptionGroup as eg:
        # DXLinkStreamer uses anyio's create_task_group() for its reader/heartbeat.
        # If the websocket is in a bad state (e.g. market closed), background task
        # cleanup can fail and wrap errors in an ExceptionGroup.
        errors = "; ".join(f"{type(e).__name__}: {e}" for e in eg.exceptions)
        await _raise_with_market_context(
            session, exchanges,
            ValueError(f"Streaming connection error for {sorted(expected)}: {errors}")
        )

    # Must check market status after streamer context is closed, since the
    # streamer's cleanup may itself raise if the connection is broken.
    if timed_out:
        missing = expected - set(events_by_symbol)
        await _raise_with_market_context(
            session, exchanges,
            ValueError(f"Timeout getting quotes after {timeout}s. No data received for: {sorted(missing)}")
        )
    return [events_by_symbol[s] for s in streamer_symbols]


async def _stream_multi_events(
    session: Session,
    event_types: list[type[Quote] | type[Greeks] | type[Summary]],
    streamer_symbols: list[str],
    timeout: float,
) -> dict[type, dict[str, Any]]:
    """Stream multiple event types concurrently on a single DXLink connection.

    Returns a dict keyed by event type, each containing a dict of symbol → event.
    Only symbols that received data within the timeout are included.
    """
    results: dict[type, dict[str, Any]] = {et: {} for et in event_types}
    expected = set(streamer_symbols)
    exchanges = _exchanges_for_symbols(streamer_symbols)
    timed_out = False

    def all_complete() -> bool:
        return all(len(results[et]) >= len(expected) for et in event_types)

    try:
        async with DXLinkStreamer(session) as streamer:
            for et in event_types:
                await streamer.subscribe(et, streamer_symbols)
            try:
                async with asyncio.timeout(timeout):
                    pending_tasks: dict[type, asyncio.Task] = {}
                    while not all_complete():
                        # Create tasks for event types still collecting
                        for et in event_types:
                            if et not in pending_tasks and len(results[et]) < len(expected):
                                pending_tasks[et] = asyncio.ensure_future(streamer.get_event(et))

                        done, _ = await asyncio.wait(
                            pending_tasks.values(),
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        for task in done:
                            event = task.result()
                            for et in event_types:
                                if pending_tasks.get(et) is task:
                                    if event.event_symbol in expected:
                                        results[et][event.event_symbol] = event
                                    del pending_tasks[et]
                                    break
            except TimeoutError:
                timed_out = True
            finally:
                for task in pending_tasks.values():
                    task.cancel()
    except ExceptionGroup as eg:
        errors = "; ".join(f"{type(e).__name__}: {e}" for e in eg.exceptions)
        await _raise_with_market_context(
            session, exchanges,
            ValueError(f"Streaming connection error for {sorted(expected)}: {errors}")
        )

    if timed_out:
        missing_info = {
            et.__name__: sorted(expected - set(results[et]))
            for et in event_types
            if len(results[et]) < len(expected)
        }
        await _raise_with_market_context(
            session, exchanges,
            ValueError(f"Timeout after {timeout}s. Missing data: {missing_info}")
        )
    return results


async def _stream_quotes_with_trade_fallback(
    session: Session,
    streamer_symbols: list[str],
    index_symbols: set[str],
    timeout: float
) -> list[Quote | Trade]:
    """Stream quotes, falling back to Trade events for index symbols.

    Some indices (e.g., VIX) don't publish Quote events in dxFeed — they only
    publish Trade events. We subscribe to both Quote and Trade for index symbols,
    preferring Quote when available but accepting Trade as a fallback.
    """
    events_by_symbol: dict[str, Quote | Trade] = {}
    expected = set(streamer_symbols)
    exchanges = _exchanges_for_symbols(streamer_symbols)
    timed_out = False
    try:
        async with DXLinkStreamer(session) as streamer:
            await streamer.subscribe(Quote, streamer_symbols)
            await streamer.subscribe(Trade, list(index_symbols))
            try:
                async with asyncio.timeout(timeout):
                    while len(events_by_symbol) < len(expected):
                        # Race Quote and Trade events — whichever arrives first wins.
                        # Quote is preferred: Trade only fills gaps for index symbols.
                        quote_task = asyncio.ensure_future(streamer.get_event(Quote))
                        trade_task = asyncio.ensure_future(streamer.get_event(Trade))
                        done, pending = await asyncio.wait(
                            [quote_task, trade_task],
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        for task in pending:
                            task.cancel()
                        for task in done:
                            event = task.result()
                            if event.event_symbol in expected:
                                if isinstance(event, Trade) and event.event_symbol in events_by_symbol:
                                    continue  # Already have a Quote for this symbol
                                events_by_symbol[event.event_symbol] = event
            except TimeoutError:
                timed_out = True
    except ExceptionGroup as eg:
        errors = "; ".join(f"{type(e).__name__}: {e}" for e in eg.exceptions)
        await _raise_with_market_context(
            session, exchanges,
            ValueError(f"Streaming connection error for {sorted(expected)}: {errors}")
        )

    if timed_out:
        missing = expected - set(events_by_symbol)
        if missing:
            await _raise_with_market_context(
                session, exchanges,
                ValueError(f"Timeout getting quotes after {timeout}s. No data received for: {sorted(missing)}")
            )
    return [events_by_symbol[s] for s in streamer_symbols]



def to_table(data: Sequence[BaseModel]) -> str:
    """Format list of Pydantic models as a plain table."""
    if not data:
        return "No data"
    return tabulate([item.model_dump() for item in data], headers='keys', tablefmt='plain')

@dataclass
class ServerContext:
    session: Session
    account: Account


def get_context(ctx: Context) -> ServerContext:
    """Extract ServerContext from the MCP request context."""
    return ctx.request_context.lifespan_context


def get_session(ctx: Context) -> Session:
    """Get the tastytrade session (auto-refreshes tokens before each API call)."""
    return get_context(ctx).session

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

    try:
        session = Session(client_secret, refresh_token)
        accounts = await Account.get(session)
        logger.info(f"Successfully authenticated with Tastytrade. Found {len(accounts)} account(s).")
    except Exception as e:
        logger.error(f"Failed to authenticate with Tastytrade: {e}")
        raise

    if account_id:
        account = next((acc for acc in accounts if acc.account_number == account_id), None)
        if not account:
            available = [acc.account_number for acc in accounts]
            raise ValueError(f"Account '{account_id}' not found. Available: {available}")
        logger.info(f"Using specified account: {account.account_number}")
    else:
        account = accounts[0]
        logger.info(f"Using default account: {account.account_number}")

    yield ServerContext(
        session=session,
        account=account
    )

mcp_app = FastMCP("TastyTrade", lifespan=lifespan)


def main() -> None:
    """CLI entry point — accepts optional transport argument (stdio, sse, streamable-http)."""
    import sys
    valid = ("stdio", "sse", "streamable-http")
    transport = sys.argv[1] if len(sys.argv) > 1 else "stdio"
    if transport not in valid:
        print(f"Invalid transport '{transport}'. Must be one of: {', '.join(valid)}", file=sys.stderr)
        sys.exit(1)
    mcp_app.run(transport)  # type: ignore[arg-type]  # validated above



@dataclass
class InstrumentDetail:
    """Details for a resolved instrument."""
    streamer_symbol: str
    instrument: Equity | Option | Future
    is_index: bool = False


class InstrumentSpec(BaseModel):
    """Specification for an instrument (stock, option, future, or index)."""
    symbol: str = Field(..., description="Symbol (e.g., 'AAPL', '/ESH26', 'SPX')")
    instrument_type: Literal['Equity', 'Future', 'Index'] | None = Field(None, description="Instrument type. Auto-detected if omitted: '/' prefix → Future, option fields → Option, otherwise Equity. Use 'Index' for index symbols like SPX, VIX, NDX.")
    option_type: Literal['C', 'P'] | None = Field(None, description="Option type: 'C' for call, 'P' for put (omit for stocks)")
    strike_price: float | None = Field(None, description="Strike price (required for options)")
    expiration_date: str | None = Field(None, description="Expiration date in YYYY-MM-DD format (required for options)")


class OrderLeg(BaseModel):
    """Specification for an order leg."""
    symbol: str = Field(..., description="Stock symbol (e.g., 'TQQQ', 'AAPL')")
    action: Literal['Buy', 'Sell', 'Buy to Open', 'Buy to Close', 'Sell to Open', 'Sell to Close'] = Field(..., description="For stocks: 'Buy' or 'Sell'. For options: 'Buy to Open', 'Buy to Close', 'Sell to Open', 'Sell to Close'")
    quantity: int = Field(..., description="Number of contracts/shares")
    option_type: Literal['C', 'P'] | None = Field(None, description="Option type: 'C' for call, 'P' for put (omit for stocks)")
    strike_price: float | None = Field(None, description="Strike price (required for options)")
    expiration_date: str | None = Field(None, description="Expiration date in YYYY-MM-DD format (required for options)")

    def to_instrument_spec(self) -> 'InstrumentSpec':
        return InstrumentSpec(
            symbol=self.symbol,
            option_type=self.option_type,
            strike_price=self.strike_price,
            expiration_date=self.expiration_date,
        )


class WatchlistSymbol(BaseModel):
    """Symbol specification for watchlist operations."""
    symbol: str = Field(..., description="Stock symbol (e.g., 'AAPL', 'TSLA')")
    instrument_type: Literal['Equity', 'Equity Option', 'Future', 'Future Option', 'Cryptocurrency', 'Warrant'] = Field(..., description="Instrument type")



def validate_date_format(date_string: str) -> date:
    """Validate date format and return date object."""
    try:
        return datetime.strptime(date_string, "%Y-%m-%d").date()
    except ValueError as e:
        raise ValueError(f"Invalid date format '{date_string}'. Expected YYYY-MM-DD format.") from e


def validate_strike_price(strike_price: Any) -> float:
    """Validate and convert strike price to float."""
    try:
        strike = float(strike_price)
    except (ValueError, TypeError) as e:
        raise ValueError(f"Invalid strike price '{strike_price}'. Expected positive number.") from e
    if strike <= 0:
        raise ValueError(f"Invalid strike price '{strike_price}'. Must be positive.")
    return strike


def _option_chain_key_builder(fn, session: Session, symbol: str):
    """Build cache key using only symbol (session changes but symbol is stable)."""
    return f"option_chain:{symbol}"


@cached(ttl=86400, cache=Cache.MEMORY, serializer=PickleSerializer(), key_builder=_option_chain_key_builder)
async def get_cached_option_chain(session: Session, symbol: str):
    """Cache option chains for 24 hours as they rarely change during that timeframe."""
    return await get_option_chain(session, symbol)


def _resolve_instrument_type(spec: InstrumentSpec) -> InstrumentType:
    """Determine instrument type from spec fields."""
    if spec.instrument_type:
        return InstrumentType(spec.instrument_type)
    if spec.option_type:
        return InstrumentType.EQUITY_OPTION
    if spec.symbol.startswith('/'):
        return InstrumentType.FUTURE
    return InstrumentType.EQUITY


async def get_instrument_details(session: Session, instrument_specs: list[InstrumentSpec]) -> list[InstrumentDetail]:
    """Get instrument details with validation and caching."""
    async def lookup_single_instrument(spec: InstrumentSpec) -> InstrumentDetail:
        symbol = spec.symbol.upper()
        resolved_type = _resolve_instrument_type(spec)

        if resolved_type == InstrumentType.EQUITY_OPTION:
            if not spec.option_type:
                raise ValueError(f"option_type ('C' or 'P') is required for option {symbol}")
            option_type = spec.option_type
            strike_price = validate_strike_price(spec.strike_price)
            expiration_date = spec.expiration_date
            if not expiration_date:
                raise ValueError(f"expiration_date is required for option {symbol}")

            target_date = validate_date_format(expiration_date)

            chain = await get_cached_option_chain(session, symbol)
            if target_date not in chain:
                available_dates = sorted(chain.keys())
                raise ValueError(f"No options found for {symbol} expiration {expiration_date}. Available: {available_dates}")

            for option in chain[target_date]:
                if (option.strike_price == strike_price and
                    option.option_type.value == option_type):
                    return InstrumentDetail(option.streamer_symbol, option)

            available_strikes = [opt.strike_price for opt in chain[target_date] if opt.option_type.value == option_type]
            raise ValueError(f"Option not found: {symbol} {expiration_date} {option_type} {strike_price}. Available strikes: {sorted(set(available_strikes))}")

        elif resolved_type == InstrumentType.FUTURE:
            instrument = await Future.get(session, symbol)
            return InstrumentDetail(instrument.streamer_symbol, instrument)

        elif resolved_type == InstrumentType.INDEX:
            instrument = await Equity.get(session, symbol)
            return InstrumentDetail(instrument.streamer_symbol, instrument, is_index=True)

        else:
            instrument = await Equity.get(session, symbol)
            return InstrumentDetail(symbol, instrument)

    return await asyncio.gather(*[lookup_single_instrument(spec) for spec in instrument_specs])


def _get_next_open_time(session, current_time: datetime) -> datetime | None:
    """Determine next market open time based on current status."""
    if session.status == MarketStatus.PRE_MARKET:
        return session.open_at
    if session.status == MarketStatus.CLOSED:
        if session.open_at and current_time < session.open_at:
            return session.open_at
        if session.close_at and current_time > session.close_at and session.next_session:
            return session.next_session.open_at
    if session.status == MarketStatus.EXTENDED and session.next_session:
        return session.next_session.open_at
    return None


def build_order_legs(instrument_details: list[InstrumentDetail], legs: list[OrderLeg]) -> list:
    """Build order legs from instrument details and leg specifications."""
    if len(instrument_details) != len(legs):
        raise ValueError(f"Mismatched legs: {len(instrument_details)} instruments vs {len(legs)} leg specs")

    built_legs = []
    for detail, leg_spec in zip(instrument_details, legs, strict=True):
        instrument = detail.instrument
        if isinstance(instrument, Equity) and instrument.is_index:
            raise ValueError(f"Cannot place orders for index symbol '{detail.streamer_symbol}' (quote-only)")
        if isinstance(instrument, (Option, Future)):
            order_action = OrderAction(leg_spec.action)
        else:
            normalized_action = leg_spec.action.lower()
            if normalized_action in {"buy", "buy to open", "buy to close"}:
                order_action = OrderAction.BUY
            elif normalized_action in {"sell", "sell to open", "sell to close"}:
                order_action = OrderAction.SELL
            else:
                raise ValueError(
                    f"Unsupported equity action '{leg_spec.action}'. "
                    "Use Buy/Sell or opening/closing variants."
                )
        built_legs.append(instrument.build_leg(Decimal(str(leg_spec.quantity)), order_action))
    return built_legs


async def calculate_net_price(ctx: Context, instrument_details: list[InstrumentDetail], legs: list[OrderLeg]) -> float:
    """Calculate net price from current market quotes."""
    session = get_session(ctx)
    quotes = await _stream_events(session, Quote, [d.streamer_symbol for d in instrument_details], timeout=10.0)

    net_price = 0.0
    for quote, detail, leg in zip(quotes, instrument_details, legs, strict=True):
        if quote.bid_price is not None and quote.ask_price is not None:
            mid_price = float(quote.bid_price + quote.ask_price) / 2
            leg_price = -mid_price if leg.action.startswith('Buy') else mid_price
            net_price += leg_price * leg.quantity
        else:
            inst = detail.instrument
            symbol_info = (
                f"{inst.underlying_symbol} {inst.option_type.value}{inst.strike_price} {inst.expiration_date}"
                if isinstance(inst, Option) else inst.symbol
            )
            logger.warning(f"Could not get bid/ask prices for {symbol_info}")
            raise ValueError(f"Could not get bid/ask for {symbol_info}")

    return round(net_price * 100) / 100



@mcp_app.tool()
async def account_overview(
    ctx: Context,
    include: list[Literal["balances", "positions"]] | None = None,
) -> dict[str, Any]:
    """
    Get account balances and/or open positions.

    Args:
        include: Sections to return (default: ["balances", "positions"]).

    Balances include net_liquidating_value, cash_balance, buying_power, margin requirements, etc.
    Use get_history(type="transactions", transaction_type="Money Movement") to separate
    trading performance from deposits/withdrawals.
    """
    if include is None:
        include = ["balances", "positions"]

    context = get_context(ctx)
    session = context.session
    result: dict[str, Any] = {}

    tasks: dict[str, Any] = {}
    if "balances" in include:
        tasks["balances"] = context.account.get_balances(session)
    if "positions" in include:
        tasks["positions"] = context.account.get_positions(session, include_marks=True)

    fetched = await asyncio.gather(*tasks.values())
    for key, value in zip(tasks.keys(), fetched):
        if key == "balances":
            result["balances"] = {k: v for k, v in value.model_dump().items() if v is not None and v != 0}
        elif key == "positions":
            result["positions"] = to_table(value)

    return result


@mcp_app.tool()
async def get_history(
    ctx: Context,
    type: Literal["transactions", "orders"],
    days: int | None = None,
    underlying_symbol: str | None = None,
    transaction_type: Literal["Trade", "Money Movement"] | None = None,
    page_offset: int = 0,
    max_results: int = 50,
) -> str:
    """
    Get transaction or order history (paginated).

    Args:
        type: "transactions" for trade/cash flow history, "orders" for filled/canceled/rejected orders.
        days: Number of days to look back (default: 90 for transactions, 7 for orders).
        underlying_symbol: Filter by underlying symbol.
        transaction_type: Filter transactions by type (only for type="transactions").
        page_offset: Starting offset for pagination (default: 0).
        max_results: Maximum number of results to return (default: 50). Use with page_offset to paginate through large result sets.
    """
    context = get_context(ctx)
    session = context.session
    effective_days = days if days is not None else (90 if type == "transactions" else 7)
    start = date.today() - timedelta(days=effective_days)

    async with rate_limiter:
        if type == "transactions":
            items = await context.account.get_history(
                session, start_date=start, underlying_symbol=underlying_symbol,
                type=transaction_type, per_page=max_results, page_offset=page_offset
            )
        else:
            items = await context.account.get_order_history(
                session, start_date=start, underlying_symbol=underlying_symbol,
                per_page=max_results, page_offset=page_offset
            )
    return to_table(items or [])


@mcp_app.tool()
async def manage_order(
    ctx: Context,
    action: Literal["place", "replace", "cancel", "list"],
    legs: list[OrderLeg] | None = None,
    price: float | None = None,
    time_in_force: Literal['Day', 'GTC', 'GTD', 'Ext', 'Ext Overnight', 'GTC Ext', 'GTC Ext Overnight', 'IOC'] = 'Day',
    dry_run: bool = False,
    order_id: str | None = None,
) -> dict[str, Any] | str:
    """
    Place, replace, cancel, or list orders.

    Actions:
        list: Get all live (active) orders. No other params needed.
        place: Place a new order. Requires 'legs'. Price auto-calculated from mid-quote if omitted.
               For debit orders use negative price (e.g., -8.50), for credit use positive (e.g., 2.25).
               If Tastytrade returns a price granularity error, retry rounded to nearest 5 cents.
        replace: Modify an existing order's price. Requires 'order_id' and 'price'.
                 For complex changes (different legs/quantities), cancel and place a new order.
        cancel: Cancel an existing order. Requires 'order_id'.

    Examples:
        List: manage_order("list")
        Auto-priced stock: manage_order("place", legs=[{"symbol": "AAPL", "action": "Buy", "quantity": 100}])
        Manual-priced option: manage_order("place", legs=[{"symbol": "TQQQ", "option_type": "C", "action": "Buy to Open", "quantity": 17, "strike_price": 100.0, "expiration_date": "2026-01-16"}], price=-8.50)
        Replace: manage_order("replace", order_id="12345", price=-10.05)
        Cancel: manage_order("cancel", order_id="12345")
    """
    context = get_context(ctx)
    session = context.session

    if action == "list":
        orders = await context.account.get_live_orders(session)
        return to_table(orders)

    if action == "place":
        if not legs:
            raise ValueError("'legs' is required for action='place'")

        async with rate_limiter:
            instrument_specs = [leg.to_instrument_spec() for leg in legs]
            instrument_details = await get_instrument_details(session, instrument_specs)

            if price is None:
                try:
                    price = await calculate_net_price(ctx, instrument_details, legs)
                    await ctx.info(f"💰 Auto-calculated net mid-price: ${price:.2f}")
                    logger.info(f"Auto-calculated price ${price:.2f} for {len(legs)}-leg order")
                except Exception as e:
                    logger.warning(f"Failed to auto-calculate price for order legs {[leg.symbol for leg in legs]}: {e!s}")
                    raise ValueError(f"Could not fetch quotes for price calculation: {e!s}. Please provide a price.") from e

            return (await context.account.place_order(
                session,
                NewOrder(
                    time_in_force=OrderTimeInForce(time_in_force),
                    order_type=OrderType.LIMIT,
                    legs=build_order_legs(instrument_details, legs),
                    price=Decimal(str(price))
                ),
                dry_run=dry_run
            )).model_dump()

    if not order_id:
        raise ValueError(f"'order_id' is required for action='{action}'")

    if action == "replace":
        if price is None:
            raise ValueError("'price' is required for action='replace'")

        async with rate_limiter:
            live_orders = await context.account.get_live_orders(session)
            existing_order = next((order for order in live_orders if str(order.id) == order_id), None)

            if not existing_order:
                live_order_ids = [str(order.id) for order in live_orders]
                logger.warning(f"Order {order_id} not found in live orders. Available orders: {live_order_ids}")
                raise ValueError(f"Order {order_id} not found in live orders")

            return (await context.account.replace_order(
                session,
                int(order_id),
                NewOrder(
                    time_in_force=existing_order.time_in_force,
                    order_type=existing_order.order_type,
                    legs=existing_order.legs,
                    price=Decimal(str(price))
                )
            )).model_dump()

    await context.account.delete_order(session, int(order_id))
    return {"success": True, "order_id": order_id}


@mcp_app.tool()
async def get_quotes(
    ctx: Context,
    instruments: list[InstrumentSpec],
    timeout: float = 10.0
) -> str:
    """
    Get live quotes for stocks, options, futures, and indices via DXLink streaming.

    Args:
        instruments: List of instrument specifications. Each contains:
            - symbol: str - Symbol (e.g., 'AAPL', '/ESH26', 'SPX')
            - instrument_type: str - Optional. Auto-detected if omitted ('/' prefix → Future).
              Use 'Index' for index symbols (SPX, VIX, NDX).
            - option_type: 'C' or 'P' (optional, omit for stocks/futures/indices)
            - strike_price: float (required for options)
            - expiration_date: str - YYYY-MM-DD format (required for options)
        timeout: Timeout in seconds

    Examples:
        Stock: get_quotes([{"symbol": "AAPL"}])
        Index: get_quotes([{"symbol": "SPX", "instrument_type": "Index"}])
        Option: get_quotes([{"symbol": "TQQQ", "option_type": "C", "strike_price": 100.0, "expiration_date": "2026-01-16"}])
    """
    if not instruments:
        raise ValueError("At least one instrument is required")

    session = get_session(ctx)
    instrument_details = await get_instrument_details(session, instruments)
    streamer_symbols = [d.streamer_symbol for d in instrument_details]
    index_symbols = {d.streamer_symbol for d in instrument_details if d.is_index}

    if index_symbols:
        events = await _stream_quotes_with_trade_fallback(session, streamer_symbols, index_symbols, timeout)
    else:
        events = await _stream_events(session, Quote, streamer_symbols, timeout)
    return to_table(events)


@mcp_app.tool()
async def get_greeks(
    ctx: Context,
    options: list[InstrumentSpec],
    timeout: float = 10.0
) -> str:
    """
    Get Greeks (delta, gamma, theta, vega, rho) for options via DXLink streaming.

    Args:
        options: List of option specifications. Each contains:
            - symbol: str - Stock symbol (e.g., 'AAPL', 'TQQQ')
            - option_type: 'C' or 'P'
            - strike_price: float
            - expiration_date: str - YYYY-MM-DD format
        timeout: Timeout in seconds
    """
    if not options:
        raise ValueError("At least one option is required")

    session = get_session(ctx)
    option_details = await get_instrument_details(session, options)

    greeks = await _stream_events(session, Greeks, [d.streamer_symbol for d in option_details], timeout)
    return to_table(greeks)


@mcp_app.tool()
async def get_gex(
    ctx: Context,
    symbol: str,
    expiration_date: str,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """
    Get Gamma Exposure (GEX) analysis for an option chain.

    Computes dealer gamma exposure per strike (gamma × OI × 100) and returns
    key levels: net GEX, gamma regime, flip level, call/put walls, and top strikes.

    Assumes standard dealer positioning: long calls (positive GEX), short puts (negative GEX).

    Args:
        symbol: Underlying symbol (e.g., 'SPY', 'SPX', 'AAPL').
        expiration_date: Expiration date in YYYY-MM-DD format.
        timeout: Timeout in seconds (default: 60). Large chains may need more time.
    """
    session = get_session(ctx)
    target_date = validate_date_format(expiration_date)

    chain = await get_cached_option_chain(session, symbol.upper())
    if target_date not in chain:
        available_dates = sorted(chain.keys())
        raise ValueError(
            f"No options for {symbol.upper()} expiration {expiration_date}. "
            f"Available: {available_dates}"
        )

    options: list[Option] = chain[target_date]
    streamer_symbols = [opt.streamer_symbol for opt in options]

    if not streamer_symbols:
        raise ValueError(f"No option contracts found for {symbol.upper()} {expiration_date}")

    opt_by_sym = {opt.streamer_symbol: opt for opt in options}

    # Stream Greeks + Summary concurrently on a single DXLink connection
    data = await _stream_multi_events(session, [Greeks, Summary], streamer_symbols, timeout)
    greeks_map = data[Greeks]
    summary_map = data[Summary]

    # Compute per-strike GEX: gamma × OI × 100 (positive for calls, negative for puts)
    strike_gex: dict[float, float] = {}
    for sym, opt in opt_by_sym.items():
        gamma_event = greeks_map.get(sym)
        summary_event = summary_map.get(sym)
        if not gamma_event or not summary_event:
            continue
        gamma = float(gamma_event.gamma) if gamma_event.gamma is not None else 0.0
        oi = summary_event.open_interest or 0
        gex = gamma * oi * 100
        strike = float(opt.strike_price)
        # Calls: positive (dealers long), Puts: negative (dealers short)
        if opt.option_type.value == "P":
            gex = -gex
        strike_gex[strike] = strike_gex.get(strike, 0.0) + gex

    if not strike_gex:
        raise ValueError(f"No GEX data available for {symbol.upper()} {expiration_date}")

    net_gex = sum(strike_gex.values())

    # GEX flip level: strike where cumulative GEX (ascending) crosses zero
    sorted_strikes = sorted(strike_gex.items())
    gex_flip_level = None
    cumulative = 0.0
    for i, (strike, gex) in enumerate(sorted_strikes):
        prev_cumulative = cumulative
        cumulative += gex
        if i > 0 and prev_cumulative * cumulative < 0:
            # Linear interpolation between the two strikes
            prev_strike = sorted_strikes[i - 1][0]
            gex_flip_level = prev_strike + (strike - prev_strike) * (-prev_cumulative / (cumulative - prev_cumulative))
            break

    # Call wall: strike with highest positive GEX
    positive_strikes = {s: g for s, g in strike_gex.items() if g > 0}
    call_wall = max(positive_strikes, key=positive_strikes.get) if positive_strikes else None

    # Put wall: strike with most negative GEX
    negative_strikes = {s: g for s, g in strike_gex.items() if g < 0}
    put_wall = min(negative_strikes, key=negative_strikes.get) if negative_strikes else None

    # Top strikes by absolute GEX
    top = sorted(strike_gex.items(), key=lambda x: abs(x[1]), reverse=True)[:5]

    result: dict[str, Any] = {
        "net_gex": round(net_gex, 2),
        "gamma_regime": "positive" if net_gex >= 0 else "negative",
        "gex_flip_level": round(gex_flip_level, 2) if gex_flip_level is not None else None,
    }
    if call_wall is not None:
        result["call_wall"] = {"strike": call_wall, "gex": round(positive_strikes[call_wall], 2)}
    if put_wall is not None:
        result["put_wall"] = {"strike": put_wall, "gex": round(negative_strikes[put_wall], 2)}
    result["top_strikes"] = [{"strike": s, "gex": round(g, 2)} for s, g in top]

    return result


@mcp_app.tool()
async def get_market_metrics(ctx: Context, symbols: list[str]) -> str:
    """
    Get market metrics including volatility (IV/HV), risk (beta, correlation),
    valuation (P/E, market cap), liquidity, dividends, earnings, and options data.

    Note extreme IV rank/percentile (0-1): low = cheap options (buy opportunity), high = expensive options (close positions).
    """
    session = get_session(ctx)
    result = await metrics.get_market_metrics(session, symbols)
    return to_table(result)


@mcp_app.tool()
async def market_status(ctx: Context, exchanges: list[Literal['Equity', 'CME', 'CFE', 'Smalls']] | None = None) -> dict[str, Any]:
    """
    Get market status for each exchange including current open/closed state,
    next opening times, holiday information, and current NYC time.
    """
    if exchanges is None:
        exchanges = ['Equity']
    session = get_session(ctx)
    market_sessions = await get_market_sessions(session, [ExchangeType(exchange) for exchange in exchanges])

    if not market_sessions:
        raise ValueError(f"No market sessions found for exchanges: {exchanges}")

    current_time = datetime.now(UTC)
    calendar = await get_market_holidays(session)
    is_holiday = current_time.date() in calendar.holidays
    is_half_day = current_time.date() in calendar.half_days

    results: list[dict[str, Any]] = []
    for ms in market_sessions:
        result: dict[str, Any] = {"exchange": ms.instrument_collection, "status": ms.status.value}

        if ms.status == MarketStatus.OPEN:
            if ms.close_at:
                result["close_at"] = ms.close_at.isoformat()
        else:
            open_at = _get_next_open_time(ms, current_time)
            if open_at:
                result["next_open"] = open_at.isoformat()
                result["time_until_open"] = humanize.naturaldelta(open_at - current_time)
            if is_holiday:
                result["is_holiday"] = True
            if is_half_day:
                result["is_half_day"] = True

        results.append(result)

    return {"current_time_nyc": now_in_new_york().isoformat(), "exchanges": results}


@mcp_app.tool()
async def search_symbols(ctx: Context, symbol: str, max_results: int = 20) -> str:
    """Search for symbols similar to the given search phrase.

    Args:
        symbol: Search phrase (e.g., 'AAPL', 'Apple').
        max_results: Maximum number of results to return (default: 20).
    """
    session = get_session(ctx)
    async with rate_limiter:
        results = await symbol_search(session, symbol)
    return to_table(results[:max_results])


@mcp_app.tool()
async def watchlist(
    ctx: Context,
    action: Literal["list", "add", "remove", "delete"],
    watchlist_type: Literal['public', 'private'] = 'private',
    name: str | None = None,
    symbols: list[WatchlistSymbol] | None = None,
) -> list[dict[str, Any]] | dict[str, Any]:
    """
    Manage watchlists: list, add symbols, remove symbols, or delete.

    Actions:
        list: Get watchlists. No name = all watchlists. With name = specific watchlist.
              Use watchlist_type to switch between 'public' and 'private'.
        add: Add symbols to a private watchlist (creates it if it doesn't exist).
        remove: Remove symbols from a private watchlist.
        delete: Delete a private watchlist by name.

    Args:
        symbols: Required for 'add' and 'remove'. Each symbol has:
            - symbol: str - e.g., "AAPL", "TSLA"
            - instrument_type: str - "Equity", "Equity Option", "Future", etc.
    """
    session = get_session(ctx)

    if action == "list":
        watchlist_class = PublicWatchlist if watchlist_type == 'public' else PrivateWatchlist
        if name:
            return [(await watchlist_class.get(session, name)).model_dump()]
        return [w.model_dump() for w in await watchlist_class.get(session)]

    effective_name = name or "main"

    if action == "delete":
        await PrivateWatchlist.remove(session, effective_name)
        return {"status": "deleted", "name": effective_name}

    if not symbols:
        raise ValueError(f"'symbols' is required for action='{action}'")

    symbol_list = ", ".join(f"{s.symbol} ({s.instrument_type})" for s in symbols)

    if action == "add":
        try:
            wl = await PrivateWatchlist.get(session, effective_name)
        except Exception:
            watchlist_entries = [{"symbol": s.symbol, "instrument_type": s.instrument_type} for s in symbols]
            watchlist_entries = [{"symbol": s.symbol, "instrument_type": s.instrument_type} for s in symbols]
            wl = PrivateWatchlist(name=effective_name, group_name="main", watchlist_entries=watchlist_entries)
            await wl.upload(session)
            logger.info(f"Created new watchlist '{effective_name}' with {len(symbols)} symbols")
            await ctx.info(f"Created watchlist '{effective_name}' and added {len(symbols)} symbols: {symbol_list}")
            return {"status": "created", "name": effective_name, "symbols_added": len(symbols)}

        for symbol_spec in symbols:
            wl.add_symbol(symbol_spec.symbol, InstrumentType(symbol_spec.instrument_type))
        await wl.update(session)
        logger.info(f"Added {len(symbols)} symbols to existing watchlist '{effective_name}'")
        await ctx.info(f"Added {len(symbols)} symbols to watchlist '{effective_name}': {symbol_list}")
        return {"status": "added", "name": effective_name, "symbols_added": len(symbols)}

    wl = await PrivateWatchlist.get(session, effective_name)
    for symbol_spec in symbols:
        wl.remove_symbol(symbol_spec.symbol, InstrumentType(symbol_spec.instrument_type))
    await wl.update(session)
    logger.info(f"Removed {len(symbols)} symbols from watchlist '{effective_name}'")
    await ctx.info(f"Removed {len(symbols)} symbols from watchlist '{effective_name}': {symbol_list}")
    return {"status": "removed", "name": effective_name, "symbols_removed": len(symbols)}



@mcp_app.prompt(title="IV Rank Analysis")
def analyze_iv_opportunities() -> list[Message]:
    return [
        UserMessage("""Please analyze IV rank, percentile, and liquidity for:
1. All active positions in my account
2. All symbols in my watchlists

Focus on identifying extremes:
- Low IV rank (<.2) may present entry opportunities (cheap options)
- High IV rank (>.8) may present exit opportunities (expensive options)
- Also consider liquidity levels to ensure tradeable positions

Use the account_overview, watchlist, and get_market_metrics tools to gather this data."""),
        AssistantMessage("""I'll analyze IV opportunities for your positions and watchlist. Let me start by gathering your current positions and watchlist data, then get market metrics for each symbol to assess IV rank extremes and liquidity.""")
    ]
