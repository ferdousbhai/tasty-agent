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

pytestmark = pytest.mark.integration

_client_secret = os.getenv("TASTYTRADE_CLIENT_SECRET")
_refresh_token = os.getenv("TASTYTRADE_REFRESH_TOKEN")

skip_no_creds = pytest.mark.skipif(
    not _client_secret or not _refresh_token,
    reason="TASTYTRADE_CLIENT_SECRET and TASTYTRADE_REFRESH_TOKEN required",
)


@pytest.fixture
def session():
    """Create a tastytrade session per test (avoids event loop conflicts)."""
    return Session(_client_secret, _refresh_token)


@pytest.fixture
async def account(session):
    """Get the first available account."""
    accounts = await Account.get(session)
    assert len(accounts) > 0, "No accounts found"
    return accounts[0]


@skip_no_creds
async def test_session_valid(session):
    """Session should be active after creation."""
    assert session.session_token is not None
    assert session.session_expiration is not None


@skip_no_creds
async def test_get_accounts(session):
    """Should fetch at least one account."""
    accounts = await Account.get(session)
    assert len(accounts) >= 1
    assert accounts[0].account_number is not None


@skip_no_creds
async def test_get_balances(session, account):
    """Should fetch account balances without error."""
    balances = await account.get_balances(session)
    assert balances is not None
    data = balances.model_dump()
    assert "net_liquidating_value" in data


@skip_no_creds
async def test_get_positions(session, account):
    """Should fetch positions (may be empty, but shouldn't error)."""
    positions = await account.get_positions(session)
    assert isinstance(positions, list)


@skip_no_creds
async def test_symbol_search(session):
    """Should find results for AAPL."""
    results = await symbol_search(session, "AAPL")
    assert len(results) > 0
    symbols = [r.symbol for r in results]
    assert "AAPL" in symbols


@skip_no_creds
async def test_get_market_metrics(session):
    """Should return metrics for AAPL."""
    metrics = await get_market_metrics(session, ["AAPL"])
    assert len(metrics) > 0
    assert metrics[0].symbol == "AAPL"


@skip_no_creds
async def test_get_option_chain(session):
    """Should return option chain with expiration dates and options."""
    chain = await get_option_chain(session, "AAPL")
    assert len(chain) > 0
    first_expiration = next(iter(chain))
    options = chain[first_expiration]
    assert len(options) > 0


@skip_no_creds
async def test_get_market_sessions(session):
    """Should return market session info for NYSE (Equity)."""
    sessions = await get_market_sessions(session, [ExchangeType.NYSE])
    assert len(sessions) > 0
    assert sessions[0].status is not None


@skip_no_creds
async def test_get_market_holidays(session):
    """Should return market calendar."""
    calendar = await get_market_holidays(session)
    assert calendar is not None
    assert hasattr(calendar, "holidays")
    assert hasattr(calendar, "half_days")


@skip_no_creds
async def test_dry_run_equity_order(session, account):
    """Should reach the API for a dry-run order (success or validation error both prove the call works)."""
    equity = await Equity.get(session, "AAPL")
    leg = equity.build_leg(Decimal("1"), OrderAction.BUY)
    order = NewOrder(
        time_in_force=OrderTimeInForce.DAY,
        order_type=OrderType.LIMIT,
        legs=[leg],
        price=Decimal("-1.00"),  # Negative = debit (buying); intentionally low so it won't fill
    )
    try:
        response = await account.place_order(session, order, dry_run=True)
        assert response is not None
    except TastytradeError as e:
        # Validation errors (margin, price) are expected — they prove the API call works
        assert "margin" in str(e).lower() or "price" in str(e).lower() or "buy" in str(e).lower()
