"""Unit tests for tasty_agent.server module."""

import asyncio
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, Mock, patch

import pytest
from tastytrade.market_sessions import MarketStatus

from tasty_agent.server import (
    InstrumentDetail,
    InstrumentSpec,
    OrderLeg,
    WatchlistSymbol,
    _get_next_open_time,
    _option_chain_key_builder,
    _stream_events,
    build_order_legs,
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

    def test_order_leg_stock(self):
        leg = OrderLeg(symbol="AAPL", action="Buy", quantity=100)
        assert leg.symbol == "AAPL"
        assert leg.action == "Buy"
        assert leg.quantity == 100

    def test_order_leg_option(self):
        leg = OrderLeg(
            symbol="AAPL",
            action="Buy to Open",
            quantity=10,
            option_type="C",
            strike_price=150.0,
            expiration_date="2024-12-20"
        )
        assert leg.action == "Buy to Open"
        assert leg.option_type == "C"

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

        with patch("tasty_agent.server.DXLinkStreamer", return_value=mock_streamer):
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

        with patch("tasty_agent.server.DXLinkStreamer", return_value=mock_streamer):
            from tastytrade.dxfeed import Quote
            result = await _stream_events(mock_session, Quote, ["AAPL", "TSLA"], timeout=5.0)

        assert result == [event_a, event_b]
