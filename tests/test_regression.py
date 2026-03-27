"""Regression tests for resolved GitHub issues.

Each test references the issue it covers. Tests that hit the live API
are marked with @pytest.mark.integration and skipped by default.
Run with: pytest -m integration (requires .env credentials)
"""

import os

import pytest
from dotenv import load_dotenv

load_dotenv()

HAS_CREDENTIALS = bool(
    os.getenv("TASTYTRADE_CLIENT_SECRET") and os.getenv("TASTYTRADE_REFRESH_TOKEN")
)

needs_credentials = pytest.mark.skipif(
    not HAS_CREDENTIALS, reason="TASTYTRADE_CLIENT_SECRET/REFRESH_TOKEN not set"
)


@pytest.fixture
async def ctx():
    """Create a fake MCP Context backed by a live TastyTrade session."""
    from tastytrade import Account, Session

    from tasty_agent.server import ServerContext

    class _ReqCtx:
        def __init__(self, sc):
            self.lifespan_context = sc

    class _Ctx:
        def __init__(self, sc):
            self.request_context = _ReqCtx(sc)

        async def info(self, msg):
            pass

    session = Session(
        os.environ["TASTYTRADE_CLIENT_SECRET"],
        os.environ["TASTYTRADE_REFRESH_TOKEN"],
    )
    accounts = await Account.get(session)
    acct_id = os.getenv("TASTYTRADE_ACCOUNT_ID")
    account = (
        next((a for a in accounts if a.account_number == acct_id), accounts[0])
        if acct_id
        else accounts[0]
    )
    return _Ctx(ServerContext(session=session, account=account))


# ---------------------------------------------------------------------------
# Issue #5: Import crash — a_get_option_chain removed in tastytrade v11
# ---------------------------------------------------------------------------

def test_issue_5_import_does_not_crash():
    """Issue #5: tasty-agent should import without errors on current tastytrade."""
    from tasty_agent.server import mcp_app

    tools = [t.name for t in mcp_app._tool_manager.list_tools()]
    assert len(tools) == 9
    assert "get_quotes" in tools
    assert "account_overview" in tools


# ---------------------------------------------------------------------------
# Issue #3: Session token expires after ~15 minutes
# tastytrade SDK v12+ auto-refreshes tokens. Verify refresh works.
# ---------------------------------------------------------------------------

@needs_credentials
@pytest.mark.integration
async def test_issue_3_session_auto_refresh():
    """Issue #3: Session should support async token refresh without crashing."""
    from tastytrade import Session

    session = Session(
        os.environ["TASTYTRADE_CLIENT_SECRET"],
        os.environ["TASTYTRADE_REFRESH_TOKEN"],
    )
    assert hasattr(session, "refresh"), "Session missing refresh() method"
    await session.refresh()


# ---------------------------------------------------------------------------
# Issue #9: get_balances crashes on float - datetime operation
# Now consolidated into account_overview.
# ---------------------------------------------------------------------------

@needs_credentials
@pytest.mark.integration
async def test_issue_9_account_overview_no_type_error(ctx):
    """Issue #9: account_overview (formerly get_balances) should not crash."""
    from tasty_agent.server import account_overview

    result = await account_overview(ctx, include=["balances", "positions"])

    assert "balances" in result
    assert "positions" in result
    bal = result["balances"]
    assert "net_liquidating_value" in bal
    assert isinstance(float(bal["net_liquidating_value"]), float)


# ---------------------------------------------------------------------------
# Issue #10: Support for futures and index quotes in get_quotes
# ---------------------------------------------------------------------------

@needs_credentials
@pytest.mark.integration
async def test_issue_10_index_quotes(ctx):
    """Issue #10: get_quotes should handle index symbols (VIX) via Trade fallback."""
    from tasty_agent.server import InstrumentSpec, get_quotes

    result = await get_quotes(
        ctx,
        instruments=[InstrumentSpec(symbol="VIX", instrument_type="Index")],
        timeout=15.0,
    )
    assert "VIX" in result
    assert "price" in result or "bid_price" in result


@needs_credentials
@pytest.mark.integration
async def test_issue_10_equity_quotes(ctx):
    """Issue #10: get_quotes should still work for plain equities."""
    from tasty_agent.server import InstrumentSpec, get_quotes

    result = await get_quotes(
        ctx,
        instruments=[InstrumentSpec(symbol="AAPL")],
        timeout=10.0,
    )
    assert "AAPL" in result
    assert "bid_price" in result
    assert "ask_price" in result


# ---------------------------------------------------------------------------
# Issue #12: get_quotes fails with unhandled TaskGroup error
# ExceptionGroup should be caught and re-raised as ValueError.
# ---------------------------------------------------------------------------

@needs_credentials
@pytest.mark.integration
async def test_issue_12_timeout_gives_valueerror_not_exceptiongroup(ctx):
    """Issue #12: Streaming timeout should raise ValueError, not ExceptionGroup."""
    from tasty_agent.server import InstrumentSpec, get_quotes

    with pytest.raises((ValueError, Exception)) as exc_info:
        await get_quotes(
            ctx,
            instruments=[InstrumentSpec(symbol="AAPL")],
            timeout=0.001,  # Impossibly short — will timeout
        )
    assert not isinstance(exc_info.value, ExceptionGroup), (
        f"Got raw ExceptionGroup instead of ValueError: {exc_info.value}"
    )


@needs_credentials
@pytest.mark.integration
async def test_issue_12_option_quotes_no_taskgroup(ctx):
    """Issue #12: Option quote streaming should not raise TaskGroup errors."""
    from tasty_agent.server import InstrumentSpec, get_quotes

    try:
        result = await get_quotes(
            ctx,
            instruments=[
                InstrumentSpec(
                    symbol="AAPL",
                    option_type="C",
                    strike_price=250.0,
                    expiration_date="2026-06-18",
                )
            ],
            timeout=15.0,
        )
        assert ".AAPL" in result
    except ValueError:
        pass  # Market closed — clean ValueError is fine
    except ExceptionGroup:
        pytest.fail("Got ExceptionGroup — issue #12 is not resolved")
