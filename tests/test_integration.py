"""Integration tests for tastytrade API calls.

Requires TASTYTRADE_CLIENT_SECRET and TASTYTRADE_REFRESH_TOKEN env vars.
All tests are skipped if credentials are not available.

Run with: uv run pytest tests/test_integration.py -v
"""

import os
from decimal import Decimal

import pytest
from tastytrade import Account, Session
from tastytrade.instruments import Equity, get_option_chain
from tastytrade.market_sessions import ExchangeType, get_market_holidays, get_market_sessions
from tastytrade.metrics import get_market_metrics
from tastytrade.order import NewOrder, OrderAction, OrderTimeInForce, OrderType
from tastytrade.search import symbol_search
from tastytrade.utils import TastytradeError

from tasty_agent.server import OrderLeg, build_order_legs, get_instrument_details

_client_secret = os.getenv("TASTYTRADE_CLIENT_SECRET")
_refresh_token = os.getenv("TASTYTRADE_REFRESH_TOKEN")

# Tastytrade represents debit prices as negative values.
NON_FILLING_DEBIT_PRICE = Decimal("-1.00")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _client_secret or not _refresh_token,
        reason="TASTYTRADE_CLIENT_SECRET and TASTYTRADE_REFRESH_TOKEN required",
    ),
]


@pytest.fixture
def session():
    """Create a tastytrade session per test (avoids event loop conflicts)."""
    return Session(_client_secret, _refresh_token)


@pytest.fixture
async def account(session):
    accounts = await Account.get(session)
    assert accounts, "No accounts found"
    return accounts[0]


async def test_session_valid(session):
    assert session.session_token is not None
    assert session.session_expiration is not None


async def test_get_accounts(session):
    accounts = await Account.get(session)
    assert accounts
    assert accounts[0].account_number is not None


async def test_get_balances(session, account):
    balances = await account.get_balances(session)
    assert balances is not None
    data = balances.model_dump()
    assert "net_liquidating_value" in data


async def test_get_positions(session, account):
    positions = await account.get_positions(session)
    assert isinstance(positions, list)


async def test_symbol_search(session):
    results = await symbol_search(session, "AAPL")
    assert results
    symbols = [r.symbol for r in results]
    assert "AAPL" in symbols


async def test_get_market_metrics(session):
    metrics = await get_market_metrics(session, ["AAPL"])
    assert metrics
    assert metrics[0].symbol == "AAPL"


async def test_get_option_chain(session):
    chain = await get_option_chain(session, "AAPL")
    assert chain
    assert next(iter(chain.values()))


async def test_get_market_sessions(session):
    sessions = await get_market_sessions(session, [ExchangeType.NYSE])
    assert sessions
    assert sessions[0].status is not None


async def test_get_market_holidays(session):
    calendar = await get_market_holidays(session)
    assert calendar is not None
    assert hasattr(calendar, "holidays")
    assert hasattr(calendar, "half_days")


async def test_dry_run_equity_order(session, account):
    equity = await Equity.get(session, "AAPL")
    leg = equity.build_leg(Decimal("1"), OrderAction.BUY)
    order = NewOrder(
        time_in_force=OrderTimeInForce.DAY,
        order_type=OrderType.LIMIT,
        legs=[leg],
        price=NON_FILLING_DEBIT_PRICE,
    )
    try:
        response = await account.place_order(session, order, dry_run=True)
        assert response is not None
    except TastytradeError as e:
        # Margin and price validation errors still prove the API call reached the broker.
        assert "margin" in str(e).lower() or "price" in str(e).lower() or "buy" in str(e).lower()


async def test_dry_run_equity_buy_to_open_order_leg_mapping(session, account):
    leg_spec = OrderLeg(symbol="AAPL", action=OrderAction.BUY_TO_OPEN, quantity=1)
    instrument_details = await get_instrument_details(session, [leg_spec.to_instrument_spec()])
    built_legs = build_order_legs(instrument_details, [leg_spec])

    order = NewOrder(
        time_in_force=OrderTimeInForce.DAY,
        order_type=OrderType.LIMIT,
        legs=built_legs,
        price=NON_FILLING_DEBIT_PRICE,
    )
    try:
        response = await account.place_order(session, order, dry_run=True)
        assert response is not None
    except TastytradeError as e:
        error_message = str(e).lower()
        assert "order_legs.action" not in error_message
        assert "margin" in error_message or "price" in error_message or "buy to open" in error_message
