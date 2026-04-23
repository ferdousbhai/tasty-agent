"""Unit tests for tasty_agent.server module."""

import asyncio
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, Mock, patch

import pytest
from tastytrade.market_sessions import ExchangeType, MarketStatus
from tastytrade.order import OrderAction

from tasty_agent.server import (
    InstrumentDetail,
    InstrumentSpec,
    OrderLeg,
    WatchlistSymbol,
    _exchanges_for_symbols,
    _get_next_open_time,
    _option_chain_key_builder,
    _stream_events,
    _stream_quotes_with_trade_fallback,
    build_order_legs,
    market_status,
    to_table,
    validate_date_format,
    validate_strike_price,
)


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

        with patch("tasty_agent.server.get_market_sessions", new=AsyncMock(return_value=[mock_market_session])), \
             patch("tasty_agent.server.get_market_holidays", new=AsyncMock(return_value=mock_calendar)), \
             patch("tasty_agent.server.now_in_new_york", return_value=datetime(2026, 4, 23, 9, 30, tzinfo=UTC)):
            result = await market_status(mock_ctx, ["Equity"])

        assert result["current_time_nyc"] == "2026-04-23T09:30:00+00:00"
        assert result["exchanges"] == [{
            "exchange": "Equity",
            "status": "Open",
            "close_at": "2026-04-23T20:00:00+00:00",
        }]


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


class TestPydanticModels:
    """Tests for Pydantic model validation."""

    def test_instrument_spec_stock(self):
        spec = InstrumentSpec(symbol="AAPL")
        assert spec.symbol == "AAPL"
        assert spec.option_type is None
        assert spec.strike_price is None
        assert spec.expiration_date is None

    def test_instrument_spec_option(self):
        spec = InstrumentSpec(
            symbol="AAPL",
            option_type="C",
            strike_price=150.0,
            expiration_date="2024-12-20"
        )
        assert spec.symbol == "AAPL"
        assert spec.option_type == "C"
        assert spec.strike_price == 150.0
        assert spec.expiration_date == "2024-12-20"

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

        with patch("tasty_agent.market_data.DXLinkStreamer", return_value=mock_streamer), \
             patch("tasty_agent.market_data.market_status_message", return_value=None):
            with pytest.raises(ValueError, match="Timeout getting quotes after"):
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
            raise ExceptionGroup("unhandled errors in a TaskGroup", [
                RuntimeError("websocket closed"),
            ])

        mock_streamer = AsyncMock()
        mock_streamer.__aenter__ = AsyncMock(side_effect=failing_context)
        mock_streamer.__aexit__ = AsyncMock(return_value=False)

        with patch("tasty_agent.market_data.DXLinkStreamer", return_value=mock_streamer), \
             patch("tasty_agent.market_data.market_status_message", return_value=None):
            with pytest.raises(ValueError, match="Streaming connection error"):
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

        with patch("tasty_agent.market_data.DXLinkStreamer", return_value=mock_streamer), \
             patch("tasty_agent.market_data.market_status_message", return_value=market_msg):
            with pytest.raises(ValueError, match="Market is currently closed"):
                from tastytrade.dxfeed import Quote
                await _stream_events(mock_session, Quote, ["AAPL"], timeout=0.1)

    @pytest.mark.asyncio
    async def test_exceptiongroup_shows_market_closed_message(self):
        """Verify market-closed message is shown for ExceptionGroup when market is closed."""
        mock_session = Mock()

        async def failing_context(*args, **kwargs):
            raise ExceptionGroup("unhandled errors in a TaskGroup", [
                RuntimeError("websocket closed"),
            ])

        mock_streamer = AsyncMock()
        mock_streamer.__aenter__ = AsyncMock(side_effect=failing_context)
        mock_streamer.__aexit__ = AsyncMock(return_value=False)

        market_msg = "Market is currently closed: CFE (closed). Live quotes are not available while the market is closed."

        with patch("tasty_agent.market_data.DXLinkStreamer", return_value=mock_streamer), \
             patch("tasty_agent.market_data.market_status_message", return_value=market_msg):
            with pytest.raises(ValueError, match="Market is currently closed"):
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
            result = await _stream_quotes_with_trade_fallback(
                mock_session, ["AAPL", "VIX"], {"VIX"}, timeout=5.0
            )

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
            result = await _stream_quotes_with_trade_fallback(
                mock_session, ["SPX"], {"SPX"}, timeout=5.0
            )

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

        with patch("tasty_agent.market_data.DXLinkStreamer", return_value=mock_streamer), \
             patch("tasty_agent.market_data.market_status_message", return_value=None):
            with pytest.raises(ValueError, match="Timeout getting quotes after"):
                await _stream_quotes_with_trade_fallback(
                    mock_session, ["VIX"], {"VIX"}, timeout=0.1
                )


class TestQuoteNaNPatch:
    """Tests for the Quote model patch that allows NaN sizes for index symbols."""

    def test_index_quotes_with_nan_sizes(self):
        """Verify index symbols (SPX, VIX) with NaN bid/ask sizes are not silently dropped."""
        from decimal import Decimal

        from tastytrade.dxfeed import Quote

        raw_data = [
            'SPX', 0, 0, 0, 0, '\x00', 0, '\x00',
            4122.49, 4123.65, 'NaN', 'NaN'
        ]
        result = Quote.from_stream(raw_data)
        assert len(result) == 1, "Index quote with NaN sizes should not be dropped"
        assert result[0].event_symbol == 'SPX'
        assert result[0].bid_price == Decimal('4122.49')
        assert result[0].ask_price == Decimal('4123.65')
        # SDK converts NaN sizes to Decimal('0') rather than None
        assert result[0].bid_size == Decimal('0')
        assert result[0].ask_size == Decimal('0')

    def test_equity_quotes_still_parse(self):
        """Verify the NaN patch doesn't break normal equity quote parsing."""
        from decimal import Decimal

        from tastytrade.dxfeed import Quote

        raw_data = ['AAPL', 0, 0, 0, 0, 'Q', 0, 'Q', 185.50, 185.55, 400, 1300]
        result = Quote.from_stream(raw_data)
        assert len(result) == 1
        assert result[0].event_symbol == 'AAPL'
        assert result[0].bid_size == Decimal('400')
        assert result[0].ask_size == Decimal('1300')

    def test_nan_prices_still_rejected(self):
        """Ensure NaN prices cause the event to be dropped (only sizes are patched)."""
        from tastytrade.dxfeed import Quote

        raw_data = ['BAD', 0, 0, 0, 0, '\x00', 0, '\x00', 'NaN', 'NaN', 'NaN', 'NaN']
        result = Quote.from_stream(raw_data)
        assert len(result) == 0, "Quote with NaN prices should be dropped"
