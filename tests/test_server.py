"""Unit tests for tasty_agent.server module."""

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from tastytrade.instruments import Equity, Future, Option, OptionType
from tastytrade.market_sessions import ExchangeType, MarketStatus
from tastytrade.order import InstrumentType, Leg, OrderAction, OrderTimeInForce

from tasty_agent.account_helpers import _compact_positions
from tasty_agent.core import to_table
from tasty_agent.market_data import (
    exchanges_for_symbols as _exchanges_for_symbols,
)
from tasty_agent.market_data import (
    get_next_open_time as _get_next_open_time,
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
    OrderSizingPolicy,
    PricingPolicy,
    _option_chain_key_builder,
    apply_order_sizing,
    build_order_legs,
    build_order_market,
    resolve_order_price,
    validate_date_format,
    validate_strike_price,
)
from tasty_agent.server import (
    _compact_greeks_event,
    _compact_market_metric,
    _compact_quote_event,
    market_status,
    place_order,
    replace_order,
)
from tasty_agent.watchlists import WatchlistSymbol, _compact_watchlist


class NoopLimiter:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *_):
        return None


class TestToTable:
    """Tests for to_table function."""

    def test_empty_data_returns_no_data(self):
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


class TestCompactToolOutputs:
    """Tests for token-efficient tool output rows."""

    def test_compact_quote_row_keeps_actionable_fields_only(self):
        event = Mock()
        event.model_dump.return_value = {
            "event_symbol": "TSLA",
            "event_time": 123,
            "sequence": 456,
            "bid_price": Decimal("10.10"),
            "ask_price": Decimal("10.30"),
            "bid_size": Decimal("12"),
            "ask_size": Decimal("9"),
        }

        row = _compact_quote_event(event)

        assert row == {
            "sym": "TSLA",
            "bid": "10.1",
            "ask": "10.3",
            "mid": "10.2",
            "bid_sz": "12",
            "ask_sz": "9",
        }

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


class TestValidateDateFormat:
    """Tests for validate_date_format function."""

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
    """Tests for validate_strike_price function."""

    def test_valid_float(self):
        assert validate_strike_price(150.0) == 150.0

    def test_valid_int(self):
        assert validate_strike_price(150) == 150.0

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


class TestOptionChainKeyBuilder:
    """Tests for cache key builder."""

    def test_key_uses_symbol_only(self):
        mock_fn = Mock()
        mock_session = Mock()
        key = _option_chain_key_builder(mock_fn, mock_session, "AAPL")
        assert key == "option_chain:AAPL"

    def test_different_sessions_same_symbol_same_key(self):
        mock_fn = Mock()
        session1 = Mock()
        session2 = Mock()
        key1 = _option_chain_key_builder(mock_fn, session1, "TSLA")
        key2 = _option_chain_key_builder(mock_fn, session2, "TSLA")
        assert key1 == key2


class TestGetNextOpenTime:
    """Tests for _get_next_open_time function."""

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


class TestMarketStatusTool:
    """Tests for the market_status MCP tool."""

    @pytest.mark.asyncio
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
            result = await market_status(mock_ctx, ["Equity"])

        assert result["current_time_nyc"] == "2026-04-23T09:30:00+00:00"
        assert result["exchanges"] == [
            {
                "exchange": "Equity",
                "status": "Open",
                "close_at": "2026-04-23T20:00:00+00:00",
            }
        ]


class TestOrderTools:
    """Tests for order tool orchestration."""

    @staticmethod
    def order_response(order_id: str):
        order = Mock()
        order.id = order_id
        order.legs = []
        order.model_dump.return_value = {"id": order_id, "status": "Live", "price": Decimal("-1.10")}
        return SimpleNamespace(
            order=order,
            buying_power_effect=None,
            fee_calculation=None,
            warnings=None,
            errors=None,
        )

    @pytest.mark.asyncio
    async def test_place_chase_recalculates_fresh_price_each_retry(self):
        broker_leg = Leg(
            instrument_type=InstrumentType.EQUITY,
            symbol="AAPL",
            action=OrderAction.BUY_TO_OPEN,
            quantity=1,
        )
        live_order = Mock(id="12345", time_in_force=OrderTimeInForce.DAY, legs=[broker_leg])
        account = Mock()
        account.place_order = AsyncMock(return_value=self.order_response("12345"))
        account.get_live_orders = AsyncMock(return_value=[live_order])
        account.replace_order = AsyncMock(return_value=self.order_response("12345"))

        mock_ctx = Mock()
        mock_ctx.request_context = Mock()
        mock_ctx.request_context.lifespan_context = Mock(session=Mock(), account=account)
        leg = OrderLeg(symbol="AAPL", action=OrderAction.BUY_TO_OPEN)

        with (
            patch(
                "tasty_agent.server._resolve_order_inputs",
                new=AsyncMock(return_value=([Mock()], [leg], Decimal("-1.10"), None)),
            ),
            patch("tasty_agent.server.build_order_legs", return_value=[broker_leg]),
            patch(
                "tasty_agent.server._resolve_replacement_market_price",
                new=AsyncMock(side_effect=[Decimal("-1.11"), Decimal("-1.12")]),
            ) as resolve_replacement,
            patch("tasty_agent.server.get_order_leg_instrument_details", new=AsyncMock(return_value=[Mock()])),
            patch("tasty_agent.server._fetch_order_market", new=AsyncMock(return_value=Mock())),
            patch("tasty_agent.server.order_price_tick_cents", return_value=1),
            patch("tasty_agent.server.CHASE_MAX_ATTEMPTS", 2),
            patch("tasty_agent.server.CHASE_INTERVAL_SECONDS", 0),
            patch("tasty_agent.server.rate_limiter", NoopLimiter()),
            patch("tasty_agent.server.asyncio.sleep", new=AsyncMock()),
        ):
            result = await place_order(mock_ctx, legs=[leg], chase=True)

        assert result["chase"] == {
            "status": "still_live",
            "checks": 2,
            "reprices": 2,
            "order_id": "12345",
            "last_step_ticks": 2,
            "last_tick_cents": 1,
            "last_price": "-1.12",
        }
        assert [call.kwargs["offset_cents"] for call in resolve_replacement.await_args_list] == [1, 2]
        assert account.get_live_orders.await_count == 2
        assert account.replace_order.await_count == 2

    @pytest.mark.asyncio
    async def test_place_order_does_not_accept_manual_price(self):
        mock_ctx = Mock()
        leg = OrderLeg(symbol="AAPL", action=OrderAction.BUY_TO_OPEN)

        with pytest.raises(TypeError, match="unexpected keyword argument 'price'"):
            await place_order(mock_ctx, legs=[leg], price=-1.10)  # type: ignore[call-arg]

    @pytest.mark.asyncio
    async def test_replace_uses_guarded_resolved_price(self):
        account = Mock()
        account.replace_order = AsyncMock(
            return_value=SimpleNamespace(
                order=None,
                buying_power_effect=None,
                fee_calculation=None,
                warnings=None,
                errors=None,
            )
        )
        mock_ctx = Mock()
        mock_ctx.request_context = Mock()
        mock_ctx.request_context.lifespan_context = Mock(session=Mock(), account=account)

        broker_leg = Leg(
            instrument_type=InstrumentType.EQUITY,
            symbol="AAPL",
            action=OrderAction.BUY_TO_OPEN,
            quantity=1,
        )
        existing_order = Mock(time_in_force=OrderTimeInForce.DAY, legs=[broker_leg])

        with (
            patch("tasty_agent.server._find_live_order", new=AsyncMock(return_value=existing_order)),
            patch(
                "tasty_agent.server._resolve_replacement_price", new=AsyncMock(return_value=Decimal("-1.10"))
            ) as resolved,
            patch("tasty_agent.server.rate_limiter", NoopLimiter()),
        ):
            result = await replace_order(mock_ctx, order_id="12345")

        assert result == {}
        resolved.assert_awaited_once_with(mock_ctx, [broker_leg])
        account.replace_order.assert_awaited_once()
        new_order = account.replace_order.call_args.args[2]
        assert new_order.price == Decimal("-1.10")
        assert new_order.legs == [broker_leg]


class TestBuildOrderLegs:
    """Tests for build_order_legs function."""

    def test_mismatched_lengths_raises_error(self):
        details = [Mock(), Mock()]
        legs = [Mock()]

        with pytest.raises(ValueError, match="Mismatched legs"):
            build_order_legs(details, legs)

    def test_empty_lists_returns_empty(self):
        result = build_order_legs([], [])
        assert result == []

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
    """Tests for quote-derived order pricing safeguards."""

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
        return InstrumentDetail(symbol, instrument)

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

    def test_mid_policy_uses_exact_order_instrument_quote(self):
        leg = OrderLeg(symbol="AAPL", action=OrderAction.BUY_TO_OPEN, quantity=1)

        market = build_order_market(
            [self.detail("AAPL")],
            [leg],
            [self.quote("1.00", "1.20")],
        )
        price, warnings = resolve_order_price(market, PricingPolicy())

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
        price, warnings = resolve_order_price(market, PricingPolicy())

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
        price, warnings = resolve_order_price(market, PricingPolicy())

        assert market.natural_price == Decimal("-0.70")
        assert market.passive_price == Decimal("-0.40")
        assert price == Decimal("-0.55")
        assert warnings == []

    def test_mid_toward_natural_moves_by_offset_cents(self):
        leg = OrderLeg(symbol="AAPL", action=OrderAction.BUY_TO_OPEN, quantity=1)
        market = build_order_market([self.detail("AAPL")], [leg], [self.quote("1.00", "1.20")])

        price, warnings = resolve_order_price(
            market,
            PricingPolicy(mode="mid_toward_natural", offset_cents=2),
        )

        assert price == Decimal("-1.12")
        assert warnings == []

    def test_manual_underlying_stock_price_is_rejected_for_option_order(self):
        leg = OrderLeg(
            symbol="AAPL",
            action=OrderAction.BUY_TO_OPEN,
            quantity=1,
            option_type="C",
            strike_price=150.0,
            expiration_date="2026-12-18",
        )
        market = build_order_market([self.detail(".AAPL261218C150")], [leg], [self.quote("1.00", "1.20")])

        with pytest.raises(ValueError, match="outside the current order market"):
            resolve_order_price(market, PricingPolicy(), manual_price=150.00)

    def test_manual_price_far_from_mid_returns_warning(self):
        leg = OrderLeg(symbol="AAPL", action=OrderAction.BUY_TO_OPEN, quantity=1)
        market = build_order_market([self.detail("AAPL")], [leg], [self.quote("1.00", "1.20")])

        price, warnings = resolve_order_price(market, PricingPolicy(), manual_price=-1.18)

        assert price == Decimal("-1.18")
        assert len(warnings) == 1
        assert "warning threshold" in warnings[0]

    def test_mid_warning_threshold_scales_with_wide_spreads(self):
        leg = OrderLeg(symbol="AAPL", action=OrderAction.BUY_TO_OPEN, quantity=1)
        market = build_order_market([self.detail("AAPL")], [leg], [self.quote("1.00", "1.80")])

        price, warnings = resolve_order_price(market, PricingPolicy(), manual_price=-1.55)

        assert price == Decimal("-1.55")
        assert warnings == []

    def test_manual_boundary_price_rejected_when_inside_cent_exists(self):
        leg = OrderLeg(symbol="AAPL", action=OrderAction.BUY_TO_OPEN, quantity=1)
        market = build_order_market([self.detail("AAPL")], [leg], [self.quote("1.00", "1.20")])

        with pytest.raises(ValueError, match="strictly inside"):
            resolve_order_price(market, PricingPolicy(), manual_price=-1.20)

    def test_crossed_quote_rejected(self):
        leg = OrderLeg(symbol="AAPL", action=OrderAction.BUY_TO_OPEN, quantity=1)

        with pytest.raises(ValueError, match="Crossed quote"):
            build_order_market([self.detail("AAPL")], [leg], [self.quote("1.20", "1.00")])

    def test_policy_price_aligns_to_instrument_tick(self):
        leg = OrderLeg(symbol="/ESM26", action=OrderAction.BUY, quantity=1)
        market = build_order_market([self.future_detail("/ESM26")], [leg], [self.quote("100.00", "100.50")])

        price, warnings = resolve_order_price(market, PricingPolicy())

        assert market.tick_size == Decimal("0.25")
        assert price == Decimal("-100.25")
        assert warnings == []

    def test_manual_price_must_align_to_instrument_tick(self):
        leg = OrderLeg(symbol="/ESM26", action=OrderAction.BUY, quantity=1)
        market = build_order_market([self.future_detail("/ESM26")], [leg], [self.quote("100.00", "100.50")])

        with pytest.raises(ValueError, match="order tick"):
            resolve_order_price(market, PricingPolicy(), manual_price=-100.10)

    def test_option_tick_sizes_are_used_when_available(self):
        leg = OrderLeg(
            symbol="AAPL",
            action=OrderAction.BUY_TO_OPEN,
            option_type="C",
            strike_price=150.0,
            expiration_date="2026-12-18",
        )
        detail = self.option_detail(".AAPL261218C150")
        detail.tick_sizes = [SimpleNamespace(value=Decimal("0.05"), threshold=None)]

        market = build_order_market([detail], [leg], [self.quote("1.00", "1.20")])

        assert market.tick_size == Decimal("0.05")

    def test_target_value_sizes_option_contract_quantity(self):
        leg = OrderLeg(symbol="TSLA", action=OrderAction.BUY_TO_OPEN)
        sizing = OrderSizingPolicy(target_value=Decimal("50000"), min_quantity=1, max_quantity=None)

        sized_legs, sizing_result = apply_order_sizing(
            [self.option_detail("TSLA_300C")],
            [leg],
            Decimal("-10"),
            sizing,
        )

        assert sized_legs[0].quantity == 50
        assert sizing_result is not None
        assert sizing_result.quantity == 50
        assert sizing_result.unit_value == Decimal("1000")
        assert sizing_result.estimated_value == Decimal("50000")

    def test_target_value_sizes_equity_share_quantity(self):
        leg = OrderLeg(symbol="TSLA", action=OrderAction.BUY_TO_OPEN)
        sizing = OrderSizingPolicy(target_value=Decimal("50000"), min_quantity=1, max_quantity=None)

        sized_legs, sizing_result = apply_order_sizing(
            [self.equity_detail("TSLA")],
            [leg],
            Decimal("-250"),
            sizing,
        )

        assert sized_legs[0].quantity == 200
        assert sizing_result is not None
        assert sizing_result.quantity == 200
        assert sizing_result.unit_value == Decimal("250")

    def test_target_value_requires_reduced_leg_ratio(self):
        leg = OrderLeg(symbol="TSLA", action=OrderAction.BUY_TO_OPEN, quantity=17)
        sizing = OrderSizingPolicy(target_value=Decimal("50000"), min_quantity=1, max_quantity=None)

        with pytest.raises(ValueError, match="smallest whole-number ratio"):
            apply_order_sizing([self.option_detail("TSLA_300C")], [leg], Decimal("-10"), sizing)


class TestPydanticModels:
    """Tests for Pydantic model validation."""

    def test_instrument_spec_stock(self):
        spec = InstrumentSpec(symbol="AAPL")
        assert spec.symbol == "AAPL"
        assert spec.option_type is None
        assert spec.strike_price is None
        assert spec.expiration_date is None

    def test_instrument_spec_option(self):
        spec = InstrumentSpec(symbol="AAPL", option_type="C", strike_price=150.0, expiration_date="2024-12-20")
        assert spec.symbol == "AAPL"
        assert spec.option_type == "C"
        assert spec.strike_price == 150.0
        assert spec.expiration_date == "2024-12-20"

    def test_option_spec_requires_option_fields_and_converts_to_instrument_spec(self):
        spec = OptionSpec(
            symbol="AAPL",
            option_type="P",
            strike_price=150.0,
            expiration_date="2024-12-20",
        )

        instrument_spec = spec.to_instrument_spec()

        assert instrument_spec.symbol == "AAPL"
        assert instrument_spec.instrument_type is None
        assert instrument_spec.option_type == "P"
        assert instrument_spec.strike_price == 150.0
        assert instrument_spec.expiration_date == "2024-12-20"

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

    def test_watchlist_symbol(self):
        ws = WatchlistSymbol(symbol="AAPL", instrument_type="Equity")
        assert ws.symbol == "AAPL"
        assert ws.instrument_type == "Equity"


class TestInstrumentDetail:
    """Tests for InstrumentDetail dataclass."""

    def test_creation(self):
        mock_instrument = Mock()
        detail = InstrumentDetail("AAPL", mock_instrument)
        assert detail.streamer_symbol == "AAPL"
        assert detail.instrument == mock_instrument


class TestExchangesForSymbols:
    """Tests for _exchanges_for_symbols helper."""

    def test_equity_symbols(self):
        assert _exchanges_for_symbols(["AAPL", "TSLA"]) == {ExchangeType.NYSE}

    def test_futures_cme(self):
        assert _exchanges_for_symbols(["/ESM26:XCME"]) == {ExchangeType.CME}

    def test_futures_cfe(self):
        assert _exchanges_for_symbols(["/VXJ26:XCBF"]) == {ExchangeType.CFE}

    def test_vx_prefix_without_xcbf(self):
        assert _exchanges_for_symbols(["/VXJ26"]) == {ExchangeType.CFE}

    def test_mixed_symbols(self):
        result = _exchanges_for_symbols(["AAPL", "/ESM26:XCME", "/VXJ26:XCBF"])
        assert result == {ExchangeType.NYSE, ExchangeType.CME, ExchangeType.CFE}


class TestStreamEvents:
    """Tests for _stream_events timeout handling (issue #12)."""

    @pytest.mark.asyncio
    async def test_timeout_raises_valueerror_not_exceptiongroup(self):
        """Verify timeout produces a clean ValueError, not an ExceptionGroup."""
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

    @pytest.mark.asyncio
    async def test_returns_events_in_order(self):
        """Verify events are returned in the same order as input symbols."""
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

    @pytest.mark.asyncio
    async def test_exceptiongroup_from_streamer_cleanup_produces_valueerror(self):
        """Verify ExceptionGroup from DXLinkStreamer cleanup is caught and converted."""
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

    @pytest.mark.asyncio
    async def test_timeout_shows_market_closed_message(self):
        """Verify market-closed message is shown instead of generic timeout."""
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

    @pytest.mark.asyncio
    async def test_exceptiongroup_shows_market_closed_message(self):
        """Verify market-closed message is shown for ExceptionGroup when market is closed."""
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
    """Tests for _stream_quotes_with_trade_fallback (VIX Trade fallback, issue #10)."""

    @pytest.mark.asyncio
    async def test_vix_gets_trade_when_no_quote(self):
        """VIX should get a Trade event when no Quote event is published."""
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

    @pytest.mark.asyncio
    async def test_quote_preferred_over_trade(self):
        """If both Quote and Trade arrive for an index, Quote should win."""
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

    @pytest.mark.asyncio
    async def test_mixed_symbols_aapl_es_vix(self):
        """Mixed query: AAPL (equity Quote), /ESM26 (futures Quote), VIX (Trade fallback)."""
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

    @pytest.mark.asyncio
    async def test_timeout_raises_valueerror(self):
        """Timeout with missing symbols should raise ValueError."""
        mock_session = Mock()

        async def block_forever(_):
            await asyncio.sleep(999)

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


class TestQuoteNaNPatch:
    """Tests for the Quote model patch that allows NaN sizes for index symbols."""

    def test_index_quotes_with_nan_sizes(self):
        """Verify index symbols (SPX, VIX) with NaN bid/ask sizes are not silently dropped."""
        from decimal import Decimal

        from tastytrade.dxfeed import Quote

        raw_data = ["SPX", 0, 0, 0, 0, "\x00", 0, "\x00", 4122.49, 4123.65, "NaN", "NaN"]
        result = Quote.from_stream(raw_data)
        assert len(result) == 1, "Index quote with NaN sizes should not be dropped"
        assert result[0].event_symbol == "SPX"
        assert result[0].bid_price == Decimal("4122.49")
        assert result[0].ask_price == Decimal("4123.65")
        # SDK converts NaN sizes to Decimal('0') rather than None
        assert result[0].bid_size == Decimal("0")
        assert result[0].ask_size == Decimal("0")

    def test_equity_quotes_still_parse(self):
        """Verify the NaN patch doesn't break normal equity quote parsing."""
        from decimal import Decimal

        from tastytrade.dxfeed import Quote

        raw_data = ["AAPL", 0, 0, 0, 0, "Q", 0, "Q", 185.50, 185.55, 400, 1300]
        result = Quote.from_stream(raw_data)
        assert len(result) == 1
        assert result[0].event_symbol == "AAPL"
        assert result[0].bid_size == Decimal("400")
        assert result[0].ask_size == Decimal("1300")

    def test_nan_prices_still_rejected(self):
        """Ensure NaN prices cause the event to be dropped (only sizes are patched)."""
        from tastytrade.dxfeed import Quote

        raw_data = ["BAD", 0, 0, 0, 0, "\x00", 0, "\x00", "NaN", "NaN", "NaN", "NaN"]
        result = Quote.from_stream(raw_data)
        assert len(result) == 0, "Quote with NaN prices should be dropped"
