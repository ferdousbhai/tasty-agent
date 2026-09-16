import asyncio
import json
import re
from datetime import UTC, date, datetime
from decimal import Decimal
from html import unescape
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from tastytrade import Account
from tastytrade.dxfeed import Quote, Trade
from tastytrade.instruments import Equity, Future, FutureOption, Option, OptionType, TickSize
from tastytrade.market_sessions import ExchangeType, MarketStatus
from tastytrade.order import InstrumentType, Leg, OrderAction, OrderTimeInForce

from tasty_agent.account_helpers import _compact_positions
from tasty_agent.core import compact_value, select_account, to_table
from tasty_agent.market_data import (
    exchanges_for_symbols as _exchanges_for_symbols,
)
from tasty_agent.market_data import (
    get_next_open_time as _get_next_open_time,
)
from tasty_agent.market_data import (
    market_status_message as _market_status_message,
)
from tasty_agent.market_data import (
    stream_events as _stream_events,
)
from tasty_agent.market_data import (
    stream_quotes_with_trade_fallback as _stream_quotes_with_trade_fallback,
)
from tasty_agent.orders import (
    InstrumentDetail,
    InstrumentSpec,
    OptionSpec,
    OrderLeg,
    _option_chain_key_builder,
    apply_order_sizing,
    build_order_legs,
    build_order_market,
    get_option_instrument_details,
    resolve_order_price,
    validate_date_format,
    validate_strike_price,
)
from tasty_agent.server import (
    _compact_greeks_event,
    _compact_market_metric,
    _compact_order_response,
    _compact_quote_event,
    get_greeks,
    market_status,
    place_order,
    replace_order,
    tool_xml,
)
from tasty_agent.watchlists import WatchlistSymbol, _compact_watchlist, manage_watchlist


class NoopLimiter:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *_):
        return None


def parse_tool_xml(text: str, tool_name: str):
    match = re.fullmatch(rf"<{tool_name}(?:\s[^>]*)?>([\s\S]*)</{tool_name}>", text)
    assert match is not None
    return json.loads(unescape(match.group(1)))


def broker_order_response(leg: Leg):
    order = Mock(legs=[leg])
    order.model_dump.return_value = {
        "id": 12345,
        "status": "Received",
        "underlying_symbol": leg.symbol,
        "order_type": "Limit",
        "time_in_force": "Day",
        "price": Decimal("-1.10"),
        "size": leg.quantity,
    }
    buying_power_effect = Mock()
    buying_power_effect.model_dump.return_value = {"change_in_buying_power": Decimal("0")}
    return SimpleNamespace(
        order=order,
        buying_power_effect=buying_power_effect,
        fee_calculation=None,
        warnings=None,
        errors=None,
    )


class TestToTable:
    def test_empty_data_returns_no_data(self):
        # to_table's early return is what list_orders, get_history and get_quotes
        # emit when the broker returns nothing; tabulate would render "".
        assert to_table([]) == "No data"

    def test_formats_pydantic_models(self):
        specs = [
            InstrumentSpec(symbol="AAPL"),
            InstrumentSpec(symbol="TSLA"),
        ]
        result = to_table(specs)
        assert "AAPL" in result
        assert "TSLA" in result
        assert "option_type" not in result

    def test_compact_value_preserves_null_list_positions(self):
        assert compact_value([Decimal("1.0"), None, Decimal("2.0")]) == ["1", None, "2"]


class TestSelectAccount:
    def test_requires_account_id_when_credentials_expose_multiple_accounts(self):
        accounts = [
            Account.model_construct(account_number="one"),
            Account.model_construct(account_number="two"),
        ]

        with pytest.raises(ValueError, match="TASTYTRADE_ACCOUNT_ID is required"):
            select_account(accounts, None)

    def test_selects_only_account_without_configuration(self):
        account = Account.model_construct(account_number="one")

        assert select_account([account], None) is account

    def test_rejects_unknown_configured_account(self):
        with pytest.raises(ValueError, match="not found"):
            select_account([Account.model_construct(account_number="one")], "missing")


class TestCompactToolOutputs:
    def test_tool_xml_wraps_json_concisely(self):
        result = tool_xml("get_quotes", {"status": "Open", "note": "A&B"})

        assert result == '<quotes>{"status":"Open","note":"A&amp;B"}</quotes>'
        assert parse_tool_xml(result, "quotes") == {"status": "Open", "note": "A&B"}

    def test_tool_xml_rejects_unserializable_values(self):
        with pytest.raises(TypeError):
            tool_xml("get_quotes", {"value": object()})

    def test_tool_xml_rejects_non_finite_numbers(self):
        with pytest.raises(ValueError, match="JSON compliant"):
            tool_xml("get_quotes", {"value": float("nan")})

    def test_tool_xml_rejects_unknown_tool_name(self):
        with pytest.raises(KeyError):
            tool_xml("typo", {})

    def test_compact_quote_row_keeps_actionable_fields_only(self):
        event = Quote.model_construct(
            event_symbol="TSLA",
            event_time=123,
            sequence=456,
            bid_price=Decimal("10.10"),
            ask_price=Decimal("10.30"),
            bid_size=Decimal("12"),
            ask_size=Decimal("9"),
        )

        row = _compact_quote_event(event)

        assert row == {
            "sym": "TSLA",
            "bid": "10.1",
            "ask": "10.3",
            "mid": "10.2",
            "bid_sz": "12",
            "ask_sz": "9",
        }

    def test_compact_quote_rejects_missing_side_instead_of_using_last_price(self):
        event = Quote.model_construct(
            event_symbol="TSLA",
            bid_price=Decimal("10.10"),
        )

        with pytest.raises(ValueError, match="ask price"):
            _compact_quote_event(event)

    def test_compact_trade_uses_native_trade_fields(self):
        event = Trade.model_construct(
            event_symbol="$SPX",
            price=Decimal("6500.25"),
            change=Decimal("12.50"),
            size=2,
            day_volume=100,
        )

        assert _compact_quote_event(event) == {
            "sym": "$SPX",
            "last": "6500.25",
            "chg": "12.5",
            "size": 2,
            "vol": 100,
        }

    def test_compact_quote_rejects_wrong_event_type(self):
        event = Mock()
        event.model_dump.return_value = {
            "event_symbol": "TSLA",
            "bid_price": Decimal("10.10"),
            "ask_price": Decimal("10.30"),
        }

        with pytest.raises(TypeError, match="Unsupported market-data event"):
            _compact_quote_event(event)

    def test_compact_order_response_rejects_missing_required_sections(self):
        with pytest.raises(ValueError, match="missing required order or buying-power data"):
            _compact_order_response(SimpleNamespace(order=None, buying_power_effect=None))

    def test_compact_order_response_preserves_broker_rejection_details(self):
        error = SimpleNamespace(code="invalid-price", message="Price is off tick")
        response = SimpleNamespace(order=None, buying_power_effect=None, warnings=None, errors=[error])

        with pytest.raises(ValueError, match="Broker rejected order: invalid-price: Price is off tick"):
            _compact_order_response(response)

    def test_compact_greeks_row_omits_stream_metadata(self):
        event = Mock()
        event.model_dump.return_value = {
            "event_symbol": ".TSLA260116C300",
            "event_time": 123,
            "sequence": 456,
            "price": Decimal("10.20"),
            "volatility": Decimal("0.54321"),
            "delta": Decimal("0.45"),
            "gamma": Decimal("0.02"),
            "theta": Decimal("-0.03"),
            "vega": Decimal("0.12"),
            "rho": Decimal("0.01"),
        }

        row = _compact_greeks_event(event)

        assert row["sym"] == ".TSLA260116C300"
        assert row["iv"] == "0.54321"
        assert "event_time" not in row
        assert "sequence" not in row

    def test_compact_greeks_rejects_missing_symbol(self):
        event = Mock()
        event.model_dump.return_value = {"delta": Decimal("0.45")}

        with pytest.raises(ValueError, match="missing event_symbol"):
            _compact_greeks_event(event)

    def test_compact_market_metric_omits_nested_option_iv_surface(self):
        earnings = Mock()
        earnings.expected_report_date = date(2026, 1, 20)
        metric = Mock()
        metric.earnings = earnings
        metric.model_dump.return_value = {
            "symbol": "TSLA",
            "implied_volatility_index_rank": "0.21",
            "implied_volatility_percentile": "0.33",
            "implied_volatility_30_day": Decimal("0.55"),
            "historical_volatility_30_day": Decimal("0.45"),
            "option_expiration_implied_volatilities": [{"large": "surface"}],
            "market_cap": Decimal("1000000000"),
            "beta": Decimal("1.2"),
        }

        row = _compact_market_metric(metric)

        assert row["symbol"] == "TSLA"
        assert row["iv_rank"] == "0.21"
        assert row["earnings"] == "2026-01-20"
        assert "option_expiration_implied_volatilities" not in row

    def test_compact_market_metric_rejects_missing_symbol(self):
        metric = Mock(earnings=None)
        metric.model_dump.return_value = {"beta": Decimal("1.2")}

        with pytest.raises(ValueError, match="missing symbol"):
            _compact_market_metric(metric)

    def test_compact_positions_returns_structured_rows(self):
        position = Mock()
        position.model_dump.return_value = {
            "symbol": "TSLA",
            "instrument_type": "Equity Option",
            "underlying_symbol": "TSLA",
            "quantity": Decimal("2"),
            "quantity_direction": "Long",
            "average_open_price": Decimal("10.50"),
            "mark_price": Decimal("11.00"),
            "realized_day_gain": Decimal("0"),
            "expires_at": date(2026, 1, 16),
        }

        rows = _compact_positions([position])

        assert rows == [
            {
                "symbol": "TSLA",
                "type": "Equity Option",
                "underlying": "TSLA",
                "qty": "2",
                "dir": "Long",
                "avg_open": "10.5",
                "mark": "11",
                "expires": "2026-01-16",
            }
        ]

    def test_compact_watchlist_metadata_omits_symbols_until_named_fetch(self):
        watchlist = Mock()
        watchlist.model_dump.return_value = {
            "name": "tech",
            "group_name": "main",
            "watchlist_entries": [
                {"symbol": "TSLA", "instrument_type": "Equity"},
                {"symbol": "NVDA", "instrument_type": "Equity"},
            ],
        }

        summary = _compact_watchlist(watchlist, include_symbols=False)
        detail = _compact_watchlist(watchlist, include_symbols=True)

        assert summary == {"name": "tech", "group": "main", "symbol_count": 2}
        assert detail["symbols"] == ["TSLA:Equity", "NVDA:Equity"]

    def test_compact_watchlist_rejects_malformed_entries(self):
        watchlist = Mock()
        watchlist.model_dump.return_value = {
            "name": "tech",
            "group_name": "main",
            "watchlist_entries": [{"symbol": "TSLA"}],
        }

        with pytest.raises(ValueError, match="instrument_type"):
            _compact_watchlist(watchlist, include_symbols=True)

    async def test_add_watchlist_propagates_lookup_failure(self):
        ctx = Mock()
        ctx.request_context.lifespan_context = SimpleNamespace(session=Mock())

        with (
            patch("tasty_agent.watchlists.PrivateWatchlist.get", new=AsyncMock(side_effect=RuntimeError("offline"))),
            pytest.raises(RuntimeError, match="offline"),
        ):
            await manage_watchlist(
                ctx,
                "add",
                name="tech",
                symbols=[WatchlistSymbol(symbol="TSLA", instrument_type="Equity")],
            )


class TestValidateDateFormat:
    def test_valid_date(self):
        result = validate_date_format("2024-12-20")
        assert result == date(2024, 12, 20)

    def test_invalid_date_format(self):
        with pytest.raises(ValueError, match="Invalid date format"):
            validate_date_format("12-20-2024")

    def test_invalid_date_value(self):
        with pytest.raises(ValueError, match="Invalid date format"):
            validate_date_format("2024-13-45")


class TestValidateStrikePrice:
    def test_valid_string_number(self):
        assert validate_strike_price("150.5") == 150.5

    def test_zero_raises_error(self):
        with pytest.raises(ValueError, match="Must be positive"):
            validate_strike_price(0)

    def test_negative_raises_error(self):
        with pytest.raises(ValueError, match="Must be positive"):
            validate_strike_price(-10)

    def test_invalid_string_raises_error(self):
        with pytest.raises(ValueError, match="Invalid strike price"):
            validate_strike_price("abc")

    def test_none_raises_error(self):
        with pytest.raises(ValueError, match="Invalid strike price"):
            validate_strike_price(None)

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_value_raises_error(self, value):
        with pytest.raises(ValueError, match="finite"):
            validate_strike_price(value)


class TestOptionChainKeyBuilder:
    def test_key_uses_symbol_only(self):
        mock_fn = Mock()
        mock_session = Mock()
        key = _option_chain_key_builder(mock_fn, mock_session, "AAPL")
        assert key == "option_chain:AAPL"


class TestGetNextOpenTime:
    def test_pre_market_returns_open_at(self):
        mock_session = Mock()
        mock_session.status = MarketStatus.PRE_MARKET
        mock_session.open_at = datetime(2024, 12, 20, 9, 30, tzinfo=UTC)

        result = _get_next_open_time(mock_session, datetime.now(UTC))
        assert result == mock_session.open_at

    def test_closed_before_open_returns_open_at(self):
        mock_session = Mock()
        mock_session.status = MarketStatus.CLOSED
        mock_session.open_at = datetime(2024, 12, 20, 14, 30, tzinfo=UTC)
        mock_session.close_at = None

        current_time = datetime(2024, 12, 20, 10, 0, tzinfo=UTC)
        result = _get_next_open_time(mock_session, current_time)
        assert result == mock_session.open_at

    def test_extended_returns_next_session_open(self):
        mock_next = Mock()
        mock_next.open_at = datetime(2024, 12, 21, 14, 30, tzinfo=UTC)

        mock_session = Mock()
        mock_session.status = MarketStatus.EXTENDED
        mock_session.next_session = mock_next

        result = _get_next_open_time(mock_session, datetime.now(UTC))
        assert result == mock_next.open_at

    def test_open_returns_none(self):
        mock_session = Mock()
        mock_session.status = MarketStatus.OPEN

        result = _get_next_open_time(mock_session, datetime.now(UTC))
        assert result is None

    async def test_market_status_lookup_failure_is_not_hidden(self):
        with (
            patch("tasty_agent.market_data.get_market_sessions", new=AsyncMock(side_effect=RuntimeError("offline"))),
            pytest.raises(RuntimeError, match="offline"),
        ):
            await _market_status_message(Mock(), {ExchangeType.NYSE})


class TestMarketStatusTool:
    async def test_market_status_returns_structured_exchange_status(self):
        mock_ctx = Mock()
        mock_ctx.request_context = Mock()
        mock_ctx.request_context.lifespan_context = Mock(session=Mock())

        mock_market_session = Mock()
        mock_market_session.instrument_collection = "Equity"
        mock_market_session.status = MarketStatus.OPEN
        mock_market_session.close_at = datetime(2026, 4, 23, 20, 0, tzinfo=UTC)

        mock_calendar = Mock(holidays=set(), half_days=set())

        with (
            patch("tasty_agent.server.get_market_sessions", new=AsyncMock(return_value=[mock_market_session])),
            patch("tasty_agent.server.get_market_holidays", new=AsyncMock(return_value=mock_calendar)),
            patch("tasty_agent.server.now_in_new_york", return_value=datetime(2026, 4, 23, 9, 30, tzinfo=UTC)),
        ):
            result = parse_tool_xml(await market_status(mock_ctx, ["Equity"]), "market_status")

        assert result["current_time_nyc"] == "2026-04-23T09:30:00+00:00"
        assert result["exchanges"] == [
            {
                "exchange": "Equity",
                "status": "Open",
                "close_at": "2026-04-23T20:00:00+00:00",
            }
        ]


class TestGreeksTool:
    async def test_get_greeks_streams_resolved_future_option_symbol(self):
        session = Mock()
        mock_ctx = Mock()
        mock_ctx.request_context = Mock()
        mock_ctx.request_context.lifespan_context = SimpleNamespace(session=session)

        option = OptionSpec(symbol="/ES", option_type="C", strike_price=5800, expiration_date="2026-05-30")
        detail = InstrumentDetail("./ESM6 C5800", Mock())
        greek = Mock()
        greek.model_dump.return_value = {
            "event_symbol": "./ESM6 C5800",
            "price": Decimal("15.25"),
            "volatility": Decimal("0.218"),
            "delta": Decimal("0.45"),
            "gamma": Decimal("0.01"),
            "theta": Decimal("-0.75"),
            "vega": Decimal("4.2"),
            "rho": Decimal("0.03"),
        }

        with (
            patch("tasty_agent.server.get_option_instrument_details", new=AsyncMock(return_value=[detail])) as resolver,
            patch("tasty_agent.server._stream_events", new=AsyncMock(return_value=[greek])) as stream,
        ):
            result = await get_greeks(mock_ctx, [option], timeout=3.0)

        resolver.assert_awaited_once_with(session, [option])
        stream.assert_awaited_once()
        stream_call = stream.await_args
        assert stream_call is not None
        assert stream_call.args[0] is session
        assert stream_call.args[2] == ["./ESM6 C5800"]
        assert stream_call.args[3] == 3.0
        assert "./ESM6 C5800" in result
        assert "<greeks>" in result


class TestOrderTools:
    async def test_place_order_does_not_accept_manual_price(self):
        mock_ctx = Mock()
        leg = OrderLeg(symbol="AAPL", action=OrderAction.BUY_TO_OPEN)

        with pytest.raises(TypeError, match="unexpected keyword argument 'price'"):
            await place_order(mock_ctx, legs=[leg], price=-1.10)  # type: ignore[call-arg]

    async def test_place_order_target_value_uses_asset_option_tick_sizes_for_msft_call(self):
        account = Mock()
        mock_ctx = Mock()
        mock_ctx.info = AsyncMock()
        mock_ctx.warning = AsyncMock()
        mock_ctx.request_context = Mock()
        mock_ctx.request_context.lifespan_context = Mock(session=Mock(), account=account)

        option = Option.model_construct(
            instrument_type=InstrumentType.EQUITY_OPTION,
            symbol=".MSFT280121C450",
            streamer_symbol=".MSFT280121C450",
            underlying_symbol="MSFT",
            shares_per_contract=100,
            option_type=OptionType.CALL,
            strike_price=450.0,
            expiration_date=date(2028, 1, 21),
        )
        underlying = Equity.model_construct(
            instrument_type=InstrumentType.EQUITY,
            symbol="MSFT",
            streamer_symbol="MSFT",
            option_tick_sizes=[
                TickSize(value=Decimal("0.01"), threshold=None),
                TickSize(value=Decimal("0.05"), threshold=Decimal("3.00")),
            ],
        )
        quote = SimpleNamespace(bid_price=Decimal("61.65"), ask_price=Decimal("65.00"))
        leg = OrderLeg(
            symbol="MSFT",
            action=OrderAction.BUY_TO_OPEN,
            option_type="C",
            strike_price=450.0,
            expiration_date="2028-01-21",
        )
        built_leg = Leg(
            instrument_type=InstrumentType.EQUITY_OPTION,
            symbol=".MSFT280121C450",
            action=OrderAction.BUY_TO_OPEN,
            quantity=Decimal("7"),
        )
        account.place_order = AsyncMock(return_value=broker_order_response(built_leg))

        with (
            patch(
                "tasty_agent.orders.get_cached_option_chain", new=AsyncMock(return_value={date(2028, 1, 21): [option]})
            ),
            patch("tasty_agent.orders.Equity.get", new=AsyncMock(return_value=underlying)),
            patch("tasty_agent.server._stream_events", new=AsyncMock(return_value=[quote])),
            patch("tasty_agent.server.rate_limiter", NoopLimiter()),
            patch.object(Option, "build_leg", autospec=True, return_value=built_leg) as build_leg,
        ):
            result = parse_tool_xml(
                await place_order(mock_ctx, legs=[leg], target_value=50000, dry_run=True),
                "order",
            )

        assert result["sizing"] == {
            "target_value": "50000",
            "unit_value": "6335",
            "quantity": 7,
            "estimated_value": "44345",
        }
        assert result["order"]["id"] == 12345
        account.place_order.assert_awaited_once()
        placed_order = account.place_order.call_args.args[1]
        assert placed_order.price == Decimal("-63.35")
        assert placed_order.legs == [built_leg]
        assert account.place_order.call_args.kwargs["dry_run"] is True
        build_leg.assert_called_once_with(option, Decimal("7"), OrderAction.BUY_TO_OPEN)
        mock_ctx.info.assert_any_await(
            "Resolved limit price -$63.35 from mid (natural=-$65.00, mid=-$63.32, passive=-$61.65, spread=$3.35, tick=$0.05)."
        )

    async def test_replace_uses_guarded_resolved_price(self):
        account = Mock()
        mock_ctx = Mock()
        mock_ctx.request_context = Mock()
        mock_ctx.request_context.lifespan_context = Mock(session=Mock(), account=account)

        broker_leg = Leg(
            instrument_type=InstrumentType.EQUITY,
            symbol="AAPL",
            action=OrderAction.BUY_TO_OPEN,
            quantity=1,
        )
        placed_order = Mock(legs=[broker_leg])
        placed_order.model_dump.return_value = {
            "id": 12345,
            "status": "Received",
            "underlying_symbol": broker_leg.symbol,
            "order_type": "Limit",
            "time_in_force": "Day",
            "price": Decimal("-1.10"),
            "size": broker_leg.quantity,
        }
        account.replace_order = AsyncMock(return_value=placed_order)
        existing_order = Mock(time_in_force=OrderTimeInForce.DAY, legs=[broker_leg])

        with (
            patch("tasty_agent.server._find_live_order", new=AsyncMock(return_value=existing_order)),
            patch(
                "tasty_agent.server._resolve_replacement_price", new=AsyncMock(return_value=Decimal("-1.10"))
            ) as resolved,
            patch("tasty_agent.server.rate_limiter", NoopLimiter()),
        ):
            result = parse_tool_xml(await replace_order(mock_ctx, order_id="12345"), "order")

        assert result["order"]["id"] == 12345
        resolved.assert_awaited_once_with(mock_ctx, [broker_leg])
        account.replace_order.assert_awaited_once()
        new_order = account.replace_order.call_args.args[2]
        assert new_order.price == Decimal("-1.10")
        assert new_order.legs == [broker_leg]


class TestBuildOrderLegs:
    def test_mismatched_lengths_raises_error(self):
        instrument = Equity.model_construct(symbol="AAPL", is_index=False)
        details = [
            InstrumentDetail("AAPL", instrument),
            InstrumentDetail("MSFT", instrument),
        ]
        legs = [OrderLeg(symbol="AAPL", action=OrderAction.BUY_TO_OPEN)]

        with pytest.raises(ValueError, match="Mismatched legs"):
            build_order_legs(details, legs)

    def test_empty_lists_raise_error(self):
        with pytest.raises(ValueError, match="At least one order leg"):
            build_order_legs([], [])

    @pytest.mark.parametrize(
        ("symbol", "action", "expected_action", "option_fields"),
        [
            ("SPY", OrderAction.BUY_TO_OPEN, "Buy to Open", {}),
            ("/ESM26", OrderAction.BUY, "Buy", {}),
            (
                "SPY",
                OrderAction.BUY_TO_OPEN,
                "Buy to Open",
                {"option_type": "C", "strike_price": 500.0, "expiration_date": "2026-12-18"},
            ),
        ],
    )
    def test_build_order_legs_preserves_valid_action(self, symbol, action, expected_action, option_fields):
        instrument = Mock(spec=[])
        instrument.is_index = False
        instrument.build_leg = Mock(return_value="built-leg")
        detail = InstrumentDetail(symbol, instrument)
        leg = OrderLeg(symbol=symbol, action=action, quantity=10, **option_fields)

        result = build_order_legs([detail], [leg])

        assert result == ["built-leg"]
        _, built_action = instrument.build_leg.call_args.args
        assert built_action.value == expected_action


class TestOrderPricing:
    @staticmethod
    def quote(bid: str, ask: str):
        event = Mock()
        event.bid_price = Decimal(bid)
        event.ask_price = Decimal(ask)
        return event

    @staticmethod
    def detail(symbol: str) -> InstrumentDetail:
        instrument = Mock()
        instrument.symbol = symbol
        instrument.is_index = False
        return InstrumentDetail(
            symbol,
            instrument,
            tick_sizes=[TickSize(value=Decimal("0.01"), threshold=None)],
        )

    @staticmethod
    def option_detail(symbol: str) -> InstrumentDetail:
        instrument = Option.model_construct(
            instrument_type=InstrumentType.EQUITY_OPTION,
            symbol=symbol,
            streamer_symbol=symbol,
            underlying_symbol="TSLA",
            shares_per_contract=100,
            option_type=OptionType.CALL,
            strike_price=300.0,
            expiration_date=date(2026, 1, 16),
        )
        return InstrumentDetail(symbol, instrument)

    @staticmethod
    def equity_detail(symbol: str) -> InstrumentDetail:
        instrument = Equity.model_construct(
            instrument_type=InstrumentType.EQUITY,
            symbol=symbol,
            streamer_symbol=symbol,
            is_index=False,
        )
        return InstrumentDetail(symbol, instrument)

    @staticmethod
    def future_detail(symbol: str) -> InstrumentDetail:
        instrument = Future.model_construct(
            instrument_type=InstrumentType.FUTURE,
            symbol=symbol,
            streamer_symbol=symbol,
            tick_size=Decimal("0.25"),
        )
        return InstrumentDetail(symbol, instrument)

    def test_empty_order_market_is_rejected(self):
        with pytest.raises(ValueError, match="At least one order leg"):
            build_order_market([], [], [])

    def test_mid_policy_uses_exact_order_instrument_quote(self):
        leg = OrderLeg(symbol="AAPL", action=OrderAction.BUY_TO_OPEN, quantity=1)

        market = build_order_market(
            [self.detail("AAPL")],
            [leg],
            [self.quote("1.00", "1.20")],
        )
        price, warnings = resolve_order_price(market)

        assert market.natural_price == Decimal("-1.20")
        assert market.passive_price == Decimal("-1.00")
        assert market.mid_price == Decimal("-1.10")
        assert price == Decimal("-1.10")
        assert warnings == []

    def test_single_leg_price_is_per_contract_not_total_quantity(self):
        leg = OrderLeg(symbol="AAPL", action=OrderAction.BUY_TO_OPEN, quantity=17)

        market = build_order_market(
            [self.detail("AAPL")],
            [leg],
            [self.quote("1.00", "1.20")],
        )
        price, warnings = resolve_order_price(market)

        assert market.natural_price == Decimal("-1.20")
        assert market.passive_price == Decimal("-1.00")
        assert market.legs[0].quantity == Decimal("17")
        assert market.legs[0].price_quantity == Decimal("1")
        assert price == Decimal("-1.10")
        assert warnings == []

    def test_spread_price_normalizes_equal_leg_quantities(self):
        buy_leg = OrderLeg(symbol="AAPL", action=OrderAction.BUY_TO_OPEN, quantity=17)
        sell_leg = OrderLeg(symbol="AAPL", action=OrderAction.SELL_TO_OPEN, quantity=17)

        market = build_order_market(
            [self.detail("AAPL_150C"), self.detail("AAPL_155C")],
            [buy_leg, sell_leg],
            [self.quote("1.00", "1.20"), self.quote("0.50", "0.60")],
        )
        price, warnings = resolve_order_price(market)

        assert market.natural_price == Decimal("-0.70")
        assert market.passive_price == Decimal("-0.40")
        assert price == Decimal("-0.55")
        assert warnings == []

    def test_crossed_quote_rejected(self):
        leg = OrderLeg(symbol="AAPL", action=OrderAction.BUY_TO_OPEN, quantity=1)

        with pytest.raises(ValueError, match="Crossed quote"):
            build_order_market([self.detail("AAPL")], [leg], [self.quote("1.20", "1.00")])

    def test_policy_price_aligns_to_instrument_tick(self):
        leg = OrderLeg(symbol="/ESM26", action=OrderAction.BUY, quantity=1)
        market = build_order_market([self.future_detail("/ESM26")], [leg], [self.quote("100.00", "100.50")])

        price, warnings = resolve_order_price(market)

        assert market.tick_size == Decimal("0.25")
        assert price == Decimal("-100.25")
        assert warnings == []

    def test_equity_tick_sizes_use_tastytrade_asset_model(self):
        leg = OrderLeg(symbol="PENNY", action=OrderAction.BUY_TO_OPEN, quantity=100)
        detail = self.equity_detail("PENNY")
        equity = detail.instrument
        assert isinstance(equity, Equity)
        equity.tick_sizes = [
            TickSize(value=Decimal("0.0001"), threshold=None),
            TickSize(value=Decimal("0.01"), threshold=Decimal("1.00")),
        ]
        market = build_order_market([detail], [leg], [self.quote("0.1234", "0.1236")])

        price, warnings = resolve_order_price(market)

        assert market.tick_size == Decimal("0.0001")
        assert price == Decimal("-0.1235")
        assert warnings == []

    def test_equity_tick_sizes_apply_thresholds_from_asset_model(self):
        leg = OrderLeg(symbol="AAPL", action=OrderAction.BUY_TO_OPEN, quantity=100)
        detail = self.equity_detail("AAPL")
        equity = detail.instrument
        assert isinstance(equity, Equity)
        equity.tick_sizes = [
            TickSize(value=Decimal("0.0001"), threshold=None),
            TickSize(value=Decimal("0.01"), threshold=Decimal("1.00")),
        ]
        market = build_order_market([detail], [leg], [self.quote("189.991", "190.009")])

        price, warnings = resolve_order_price(market)

        assert market.tick_size == Decimal("0.01")
        assert price == Decimal("-190.00")
        assert warnings == []

    def test_option_tick_sizes_use_tastytrade_asset_model(self):
        leg = OrderLeg(
            symbol="TQQQ",
            action=OrderAction.BUY_TO_OPEN,
            option_type="C",
            strike_price=65.0,
            expiration_date="2027-01-15",
        )
        detail = self.option_detail(".TQQQ270115C65")
        detail.tick_sizes = [
            TickSize(value=Decimal("0.01"), threshold=None),
            TickSize(value=Decimal("0.05"), threshold=Decimal("3.00")),
        ]
        market = build_order_market([detail], [leg], [self.quote("19.67", "19.69")])

        price, warnings = resolve_order_price(market)

        assert market.mid_price == Decimal("-19.68")
        assert market.tick_size == Decimal("0.05")
        assert price == Decimal("-19.70")
        assert len(warnings) == 1
        assert "nearest valid tick" in warnings[0]

    def test_mid_policy_rounds_to_nearest_option_tick(self):
        leg = OrderLeg(
            symbol="AAPL",
            action=OrderAction.BUY_TO_OPEN,
            option_type="C",
            strike_price=150.0,
            expiration_date="2026-12-18",
        )
        detail = self.option_detail(".AAPL261218C150")
        detail.tick_sizes = [TickSize(value=Decimal("0.05"), threshold=None)]
        market = build_order_market([detail], [leg], [self.quote("1.11", "1.13")])

        price, warnings = resolve_order_price(market)

        assert market.mid_price == Decimal("-1.12")
        assert market.tick_size == Decimal("0.05")
        assert price == Decimal("-1.10")
        assert len(warnings) == 1
        assert "nearest valid tick" in warnings[0]

    def test_option_pricing_requires_tick_sizes(self):
        leg = OrderLeg(
            symbol="AAPL",
            action=OrderAction.BUY_TO_OPEN,
            option_type="C",
            strike_price=150.0,
            expiration_date="2026-12-18",
        )
        detail = self.option_detail(".AAPL261218C150")

        with pytest.raises(ValueError, match="Missing broker tick sizes"):
            build_order_market([detail], [leg], [self.quote("1.11", "1.13")])

    def test_equity_pricing_requires_broker_tick_sizes(self):
        leg = OrderLeg(symbol="AAPL", action=OrderAction.BUY_TO_OPEN, quantity=1)

        with pytest.raises(ValueError, match="Missing broker tick sizes"):
            build_order_market([self.equity_detail("AAPL")], [leg], [self.quote("1.11", "1.13")])

    def test_target_value_sizes_option_contract_quantity(self):
        leg = OrderLeg(symbol="TSLA", action=OrderAction.BUY_TO_OPEN)

        sized_legs, sizing_result = apply_order_sizing(
            [self.option_detail("TSLA_300C")],
            [leg],
            Decimal("-10"),
            Decimal("50000"),
        )

        assert sized_legs[0].quantity == 50
        assert sizing_result is not None
        assert sizing_result.quantity == 50
        assert sizing_result.unit_value == Decimal("1000")
        assert sizing_result.estimated_value == Decimal("50000")

    def test_target_value_sizes_equity_share_quantity(self):
        leg = OrderLeg(symbol="TSLA", action=OrderAction.BUY_TO_OPEN)

        sized_legs, sizing_result = apply_order_sizing(
            [self.equity_detail("TSLA")],
            [leg],
            Decimal("-250"),
            Decimal("50000"),
        )

        assert sized_legs[0].quantity == 200
        assert sizing_result is not None
        assert sizing_result.quantity == 200
        assert sizing_result.unit_value == Decimal("250")

    def test_target_value_scales_multi_leg_spread_ratio(self):
        buy_leg = OrderLeg(symbol="TSLA", action=OrderAction.BUY_TO_OPEN, quantity=2)
        sell_leg = OrderLeg(symbol="TSLA", action=OrderAction.SELL_TO_OPEN, quantity=1)

        sized_legs, sizing_result = apply_order_sizing(
            [self.option_detail("TSLA_300C"), self.option_detail("TSLA_320C")],
            [buy_leg, sell_leg],
            Decimal("-5"),
            Decimal("50000"),
        )

        assert [leg.quantity for leg in sized_legs] == [200, 100]
        assert sizing_result is not None
        assert sizing_result.quantity == 100
        assert sizing_result.unit_value == Decimal("500")
        assert sizing_result.estimated_value == Decimal("50000")

    def test_target_value_requires_reduced_leg_ratio(self):
        leg = OrderLeg(symbol="TSLA", action=OrderAction.BUY_TO_OPEN, quantity=17)

        with pytest.raises(ValueError, match="smallest whole-number ratio"):
            apply_order_sizing([self.option_detail("TSLA_300C")], [leg], Decimal("-10"), Decimal("50000"))


class TestPydanticModels:
    def test_instrument_spec_rejects_partial_option_identity(self):
        with pytest.raises(ValueError, match="must be supplied together"):
            InstrumentSpec(symbol="AAPL", option_type="C")

    def test_instrument_spec_rejects_conflicting_explicit_type(self):
        with pytest.raises(ValueError, match="cannot be combined"):
            InstrumentSpec(
                symbol="AAPL",
                instrument_type="Equity",
                option_type="C",
                strike_price=150,
                expiration_date="2026-12-18",
            )

    @pytest.mark.parametrize(
        ("kwargs", "expected_action"),
        [
            ({"symbol": "AAPL", "action": OrderAction.BUY_TO_OPEN, "quantity": 100}, OrderAction.BUY_TO_OPEN),
            (
                {
                    "symbol": "AAPL",
                    "action": OrderAction.BUY_TO_OPEN,
                    "quantity": 10,
                    "option_type": "C",
                    "strike_price": 150.0,
                    "expiration_date": "2024-12-20",
                },
                OrderAction.BUY_TO_OPEN,
            ),
            ({"symbol": "/ESM26", "action": OrderAction.BUY, "quantity": 1}, OrderAction.BUY),
        ],
    )
    def test_order_leg_accepts_valid_action_contract(self, kwargs, expected_action):
        leg = OrderLeg(**kwargs)
        assert leg.action == expected_action

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            (
                {"symbol": "AAPL", "action": OrderAction.BUY, "quantity": 100},
                "Equities and options must use one of: Buy to Open, Buy to Close, Sell to Open, Sell to Close.",
            ),
            (
                {
                    "symbol": "AAPL",
                    "action": OrderAction.BUY,
                    "quantity": 10,
                    "option_type": "C",
                    "strike_price": 150.0,
                    "expiration_date": "2024-12-20",
                },
                "Equities and options must use one of: Buy to Open, Buy to Close, Sell to Open, Sell to Close.",
            ),
            (
                {"symbol": "/ESM26", "action": OrderAction.BUY_TO_OPEN, "quantity": 1},
                "Futures must use 'Buy' or 'Sell'",
            ),
        ],
    )
    def test_order_leg_rejects_invalid_action_contract(self, kwargs, message):
        with pytest.raises(ValueError, match=message):
            OrderLeg(**kwargs)


class TestOptionInstrumentDetails:
    async def test_resolves_future_option_streamer_symbol(self):
        session = Mock()
        future_option = FutureOption.model_construct(
            instrument_type=InstrumentType.FUTURE_OPTION,
            symbol="./MESM6 P5800",
            streamer_symbol="./MESM6 P5800",
            underlying_symbol="/MES",
            option_type=OptionType.PUT,
            strike_price=Decimal("5800.0"),
            expiration_date=date(2026, 5, 30),
        )
        chain = {date(2026, 5, 30): [future_option]}

        with patch(
            "tasty_agent.orders.get_cached_future_option_chain", new=AsyncMock(return_value=chain)
        ) as mock_chain:
            details = await get_option_instrument_details(
                session,
                [
                    OptionSpec(
                        symbol="/mes",
                        option_type="P",
                        strike_price=5800,
                        expiration_date="2026-05-30",
                    )
                ],
            )

        assert details[0].streamer_symbol == "./MESM6 P5800"
        assert details[0].instrument == future_option
        mock_chain.assert_awaited_once_with(session, "/MES")

    async def test_future_option_missing_strike_lists_available_strikes(self):
        session = Mock()
        future_option = FutureOption.model_construct(
            instrument_type=InstrumentType.FUTURE_OPTION,
            symbol="./ESM6 C5800",
            streamer_symbol="./ESM6 C5800",
            underlying_symbol="/ES",
            option_type=OptionType.CALL,
            strike_price=Decimal("5800.0"),
            expiration_date=date(2026, 5, 30),
        )
        chain = {date(2026, 5, 30): [future_option]}

        with (
            patch("tasty_agent.orders.get_cached_future_option_chain", new=AsyncMock(return_value=chain)),
            pytest.raises(ValueError, match="Futures option not found: /ES 2026-05-30 C 5900"),
        ):
            await get_option_instrument_details(
                session,
                [
                    OptionSpec(
                        symbol="/ES",
                        option_type="C",
                        strike_price=5900,
                        expiration_date="2026-05-30",
                    )
                ],
            )


class TestExchangesForSymbols:
    def test_equity_symbols(self):
        assert _exchanges_for_symbols(["AAPL", "TSLA"]) == {ExchangeType.NYSE}

    def test_futures_cme(self):
        assert _exchanges_for_symbols(["/ESM26:XCME"]) == {ExchangeType.CME}

    def test_futures_option_cme(self):
        assert _exchanges_for_symbols(["./ESM6 C5800"]) == {ExchangeType.CME}

    def test_futures_cfe(self):
        assert _exchanges_for_symbols(["/VXJ26:XCBF"]) == {ExchangeType.CFE}

    def test_futures_option_cfe(self):
        assert _exchanges_for_symbols(["./VXJ6 C25"]) == {ExchangeType.CFE}

    def test_vx_prefix_without_xcbf(self):
        assert _exchanges_for_symbols(["/VXJ26"]) == {ExchangeType.CFE}

    def test_mixed_symbols(self):
        result = _exchanges_for_symbols(["AAPL", "/ESM26:XCME", "/VXJ26:XCBF"])
        assert result == {ExchangeType.NYSE, ExchangeType.CME, ExchangeType.CFE}


class TestStreamEvents:
    async def test_timeout_raises_valueerror_not_exceptiongroup(self):
        mock_session = Mock()

        mock_streamer = AsyncMock()
        mock_streamer.__aenter__ = AsyncMock(return_value=mock_streamer)
        mock_streamer.__aexit__ = AsyncMock(return_value=False)
        mock_streamer.subscribe = AsyncMock()

        async def block_forever(_):
            await asyncio.sleep(999)

        mock_streamer.get_event = block_forever

        with (
            patch("tasty_agent.market_data.DXLinkStreamer", return_value=mock_streamer),
            patch("tasty_agent.market_data.market_status_message", return_value=None),
            pytest.raises(ValueError, match="Timeout getting quotes after"),
        ):
            from tastytrade.dxfeed import Quote

            await _stream_events(mock_session, Quote, ["AAPL"], timeout=0.1)

    async def test_returns_events_in_order(self):
        mock_session = Mock()

        event_a = Mock()
        event_a.event_symbol = "AAPL"
        event_b = Mock()
        event_b.event_symbol = "TSLA"

        events = [event_b, event_a]
        call_count = 0

        async def fake_get_event(_):
            nonlocal call_count
            event = events[call_count]
            call_count += 1
            return event

        mock_streamer = AsyncMock()
        mock_streamer.__aenter__ = AsyncMock(return_value=mock_streamer)
        mock_streamer.__aexit__ = AsyncMock(return_value=False)
        mock_streamer.subscribe = AsyncMock()
        mock_streamer.get_event = fake_get_event

        with patch("tasty_agent.market_data.DXLinkStreamer", return_value=mock_streamer):
            from tastytrade.dxfeed import Quote

            result = await _stream_events(mock_session, Quote, ["AAPL", "TSLA"], timeout=5.0)

        assert result == [event_a, event_b]

    async def test_exceptiongroup_from_streamer_cleanup_produces_valueerror(self):
        mock_session = Mock()

        async def failing_context(*args, **kwargs):
            raise ExceptionGroup(
                "unhandled errors in a TaskGroup",
                [
                    RuntimeError("websocket closed"),
                ],
            )

        mock_streamer = AsyncMock()
        mock_streamer.__aenter__ = AsyncMock(side_effect=failing_context)
        mock_streamer.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("tasty_agent.market_data.DXLinkStreamer", return_value=mock_streamer),
            patch("tasty_agent.market_data.market_status_message", return_value=None),
            pytest.raises(ValueError, match="Streaming connection error"),
        ):
            from tastytrade.dxfeed import Quote

            await _stream_events(mock_session, Quote, ["SPX"], timeout=5.0)

    async def test_timeout_shows_market_closed_message(self):
        mock_session = Mock()

        mock_streamer = AsyncMock()
        mock_streamer.__aenter__ = AsyncMock(return_value=mock_streamer)
        mock_streamer.__aexit__ = AsyncMock(return_value=False)
        mock_streamer.subscribe = AsyncMock()

        async def block_forever(_):
            await asyncio.sleep(999)

        mock_streamer.get_event = block_forever

        market_msg = "Market is currently closed: Equity (opens in 14 hours). Live quotes are not available while the market is closed."

        with (
            patch("tasty_agent.market_data.DXLinkStreamer", return_value=mock_streamer),
            patch("tasty_agent.market_data.market_status_message", return_value=market_msg),
            pytest.raises(ValueError, match="Market is currently closed"),
        ):
            from tastytrade.dxfeed import Quote

            await _stream_events(mock_session, Quote, ["AAPL"], timeout=0.1)

    async def test_exceptiongroup_shows_market_closed_message(self):
        mock_session = Mock()

        async def failing_context(*args, **kwargs):
            raise ExceptionGroup(
                "unhandled errors in a TaskGroup",
                [
                    RuntimeError("websocket closed"),
                ],
            )

        mock_streamer = AsyncMock()
        mock_streamer.__aenter__ = AsyncMock(side_effect=failing_context)
        mock_streamer.__aexit__ = AsyncMock(return_value=False)

        market_msg = (
            "Market is currently closed: CFE (closed). Live quotes are not available while the market is closed."
        )

        with (
            patch("tasty_agent.market_data.DXLinkStreamer", return_value=mock_streamer),
            patch("tasty_agent.market_data.market_status_message", return_value=market_msg),
            pytest.raises(ValueError, match="Market is currently closed"),
        ):
            from tastytrade.dxfeed import Quote

            await _stream_events(mock_session, Quote, ["/VXJ26:XCBF"], timeout=5.0)


class TestStreamQuotesWithTradeFallback:
    async def test_vix_gets_trade_when_no_quote(self):
        mock_session = Mock()

        trade_event = Mock()
        trade_event.event_symbol = "VIX"

        quote_event = Mock()
        quote_event.event_symbol = "AAPL"

        async def fake_get_event(event_type):
            from tastytrade.dxfeed import Quote, Trade

            if event_type is Trade:
                return trade_event
            if event_type is Quote:
                return quote_event
            await asyncio.sleep(999)

        mock_streamer = AsyncMock()
        mock_streamer.__aenter__ = AsyncMock(return_value=mock_streamer)
        mock_streamer.__aexit__ = AsyncMock(return_value=False)
        mock_streamer.subscribe = AsyncMock()
        mock_streamer.get_event = fake_get_event

        with patch("tasty_agent.market_data.DXLinkStreamer", return_value=mock_streamer):
            result = await _stream_quotes_with_trade_fallback(mock_session, ["AAPL", "VIX"], {"VIX"}, timeout=5.0)

        assert result == [quote_event, trade_event]

    async def test_quote_preferred_over_trade(self):
        from tastytrade.dxfeed import Trade

        mock_session = Mock()

        quote_spx = Mock()
        quote_spx.event_symbol = "SPX"

        trade_spx = Mock(spec=Trade)
        trade_spx.event_symbol = "SPX"

        call_count = 0

        async def fake_get_event(event_type):
            nonlocal call_count
            from tastytrade.dxfeed import Quote, Trade

            call_count += 1
            if event_type is Quote and call_count <= 2:
                return quote_spx
            if event_type is Trade:
                return trade_spx
            await asyncio.sleep(999)

        mock_streamer = AsyncMock()
        mock_streamer.__aenter__ = AsyncMock(return_value=mock_streamer)
        mock_streamer.__aexit__ = AsyncMock(return_value=False)
        mock_streamer.subscribe = AsyncMock()
        mock_streamer.get_event = fake_get_event

        with patch("tasty_agent.market_data.DXLinkStreamer", return_value=mock_streamer):
            result = await _stream_quotes_with_trade_fallback(mock_session, ["SPX"], {"SPX"}, timeout=5.0)

        assert result == [quote_spx]

    async def test_mixed_symbols_aapl_es_vix(self):
        mock_session = Mock()

        quote_aapl = Mock()
        quote_aapl.event_symbol = "AAPL"
        quote_es = Mock()
        quote_es.event_symbol = "/ESM26:XCME"
        trade_vix = Mock()
        trade_vix.event_symbol = "VIX"

        quote_events = iter([quote_aapl, quote_es])

        async def fake_get_event(event_type):
            from tastytrade.dxfeed import Quote, Trade

            if event_type is Quote:
                try:
                    return next(quote_events)
                except StopIteration:
                    await asyncio.sleep(999)
            if event_type is Trade:
                return trade_vix
            await asyncio.sleep(999)

        mock_streamer = AsyncMock()
        mock_streamer.__aenter__ = AsyncMock(return_value=mock_streamer)
        mock_streamer.__aexit__ = AsyncMock(return_value=False)
        mock_streamer.subscribe = AsyncMock()
        mock_streamer.get_event = fake_get_event

        with patch("tasty_agent.market_data.DXLinkStreamer", return_value=mock_streamer):
            result = await _stream_quotes_with_trade_fallback(
                mock_session,
                ["AAPL", "/ESM26:XCME", "VIX"],
                {"VIX"},
                timeout=5.0,
            )

        assert result == [quote_aapl, quote_es, trade_vix]

    async def test_timeout_raises_valueerror(self):
        mock_session = Mock()
        cancelled_tasks = 0

        async def block_forever(_):
            nonlocal cancelled_tasks
            try:
                await asyncio.sleep(999)
            except asyncio.CancelledError:
                cancelled_tasks += 1
                raise

        mock_streamer = AsyncMock()
        mock_streamer.__aenter__ = AsyncMock(return_value=mock_streamer)
        mock_streamer.__aexit__ = AsyncMock(return_value=False)
        mock_streamer.subscribe = AsyncMock()
        mock_streamer.get_event = block_forever

        with (
            patch("tasty_agent.market_data.DXLinkStreamer", return_value=mock_streamer),
            patch("tasty_agent.market_data.market_status_message", return_value=None),
            pytest.raises(ValueError, match="Timeout getting quotes after"),
        ):
            await _stream_quotes_with_trade_fallback(mock_session, ["VIX"], {"VIX"}, timeout=0.1)

        assert cancelled_tasks == 2
