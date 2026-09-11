"""Integration tests for tastytrade API calls.

Requires TASTYTRADE_CLIENT_SECRET and TASTYTRADE_REFRESH_TOKEN env vars.
All tests are skipped if credentials are not available.

Run with: uv run pytest tests/test_integration.py -v
"""

import os
from decimal import Decimal

import pytest
from tastytrade import Account, Session
from tastytrade.order import NewOrder, OrderAction, OrderTimeInForce, OrderType
from tastytrade.utils import TastytradeError

from tasty_agent.orders import OrderLeg, build_order_legs, get_instrument_details

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
