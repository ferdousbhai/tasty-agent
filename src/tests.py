import asyncio
import importlib
import warnings
import traceback
from datetime import timedelta, date, datetime
from time import sleep
from uuid import UUID
from typing import AsyncGenerator
import pytest
import pytest_asyncio
from .tasty_agent.server import (
    schedule_trade,
    list_scheduled_trades,
    remove_scheduled_trade,
    plot_nlv_history,
    get_account_balances,
    get_open_positions,
    get_transaction_history,
    get_metrics,
    get_prices,
)
from src.tasty_agent import state as tasty_state
import src.tasty_agent.server as tasty_server


def warning_handler(message, category, filename, lineno, file=None, line=None):
    print('\nWarning:\n')
    print(f'{category.__name__}: {message}')
    print('Stack trace:')
    traceback.print_stack()

warnings.showwarning = warning_handler

@pytest_asyncio.fixture
async def scheduled_trade() -> AsyncGenerator[str, None]:
    """Fixture that creates a test trade and returns its task ID."""
    result = await schedule_trade(
        action="Buy to Open",
        quantity=1,
        underlying_symbol="SPY",
        dry_run=True
    )
    task_id = result.split()[1]
    yield task_id
    # Cleanup
    await remove_scheduled_trade(task_id)

@pytest_asyncio.fixture(scope="function")
async def event_loop():
    """Create an instance of the default event loop for each test case."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()

@pytest.fixture(autouse=True)
def reset_global_state():
    # Reset the cached AccountState instance.
    tasty_state.AccountState._instance = None
    # Reload the modules so that their globals (like account_state) are recreated
    importlib.reload(tasty_state)
    importlib.reload(tasty_server)
    yield
    # Do the same cleanup after the test to ensure a clean slate for the next one.
    tasty_state.AccountState._instance = None
    importlib.reload(tasty_state)
    importlib.reload(tasty_server)

class TestTradeScheduling:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("trade_params", [
        {
            "action": "Buy to Open",
            "quantity": 1,
            "underlying_symbol": "SPY",
            "dry_run": True
        },
        {
            "action": "Buy to Open",
            "quantity": 1,
            "underlying_symbol": "SPY",
            "strike": 400,
            "option_type": "P",
            "expiration_date": None,  # Will be set in the test
            "dry_run": True
        }
    ])
    async def test_schedule_trade(self, trade_params: dict) -> None:
        """Test scheduling stock and option trades."""
        if "expiration_date" in trade_params and trade_params["expiration_date"] is None:
            trade_params["expiration_date"] = get_expiration_date()

        result = await schedule_trade(**trade_params)
        assert "scheduled successfully" in result.lower()
        task_id = result.split()[1]
        assert UUID(task_id)  # Verify task_id is a valid UUID

# Test list_scheduled_trades
@pytest.mark.asyncio
async def test_list_scheduled_trades(scheduled_trade: str) -> None:
    """Test listing scheduled trades."""
    # Add a small delay to ensure the trade is scheduled
    await asyncio.sleep(0.1)
    result = await list_scheduled_trades()
    assert "Scheduled Tasks:" in result
    assert "Task ID" in result
    assert "Description" in result
    assert scheduled_trade in result  # Verify our scheduled trade is listed

# Test remove_scheduled_trade
@pytest.mark.asyncio
async def test_remove_scheduled_trade(scheduled_trade: str) -> None:
    """Test removing a scheduled trade."""
    # Test removal of valid task
    result = await remove_scheduled_trade(scheduled_trade)
    assert "cancelled successfully" in result.lower()

    # Test removing non-existent task
    fake_uuid = str(UUID('00000000-0000-0000-0000-000000000000'))
    result = await remove_scheduled_trade(fake_uuid)
    assert "not found" in result.lower()

# Test plot_nlv_history
@pytest.mark.parametrize("time_back", ['1d', '1m', '3m', '6m', '1y', 'all'])
def test_plot_nlv_history_time_periods(time_back: str) -> None:
    """Test plot_nlv_history with different time periods."""
    result = plot_nlv_history(time_back=time_back)
    sleep(1)
    assert isinstance(result, str)
    assert len(result) > 0

# Test get_account_balances
@pytest.mark.asyncio
async def test_get_account_balances() -> None:
    result = await get_account_balances()
    assert "Account Balances:" in result
    assert "Cash Balance:" in result
    assert "Buying Power:" in result
    assert "Net Liquidating Value:" in result

# Test get_open_positions
@pytest.mark.asyncio
async def test_get_open_positions() -> None:
    """Test getting open positions."""
    result = await get_open_positions()
    assert isinstance(result, str)
    assert "Current Positions:" in result or "No open positions found" in result

# Test get_transaction_history
def test_get_transaction_history() -> None:
    result = get_transaction_history()
    assert isinstance(result, str)
    assert "Transaction History:" in result or "No transactions found" in result

    # Test with invalid date format
    result = get_transaction_history("invalid-date")
    assert "Invalid date format" in result

class TestMetrics:
    @pytest.mark.asyncio
    async def test_get_metrics_with_valid_symbols(self) -> None:
        """Test getting metrics with valid symbols."""
        # Create new event loop for this test
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            result = await get_metrics(["SPY", "AAPL"])
            assert "Market Metrics:" in result
            assert "SPY" in result
            assert "AAPL" in result
        except Exception as e:
            pytest.fail(f"Test failed with error: {str(e)}")
        finally:
            # Clean up
            loop.close()

class TestPrices:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("test_params,expected_msg", [
        (
            {"underlying_symbol": "SPY"},
            "Current prices for SPY"
        ),
        (
            {
                "underlying_symbol": "SPY",
                "expiration_date": None,  # Will be set in test
                "option_type": "P",
                "strike": 400
            },
            "Current prices for"
        ),
        (
            {"underlying_symbol": "INVALID"},
            "Could not find instrument"
        ),
        (
            {
                "underlying_symbol": "SPY",
                "expiration_date": "invalid-date",
                "option_type": "P",
                "strike": 400
            },
            "Invalid expiration date format"
        ),
        (
            {
                "underlying_symbol": "SPY",
                "option_type": "P"
            },
            "Could not find instrument"
        )
    ])
    async def test_get_prices(self, test_params: dict, expected_msg: str) -> None:
        """Test getting prices for various scenarios."""
        if "expiration_date" in test_params and test_params["expiration_date"] is None:
            test_params["expiration_date"] = get_expiration_date()

        result = await get_prices(**test_params)
        assert expected_msg in result
        # Only check for bid/ask if we expect a successful price lookup
        if expected_msg.startswith("Current prices for"):
            assert "Bid: $" in result
            assert "Ask: $" in result

def get_expiration_date(target_date: str | None = None) -> str:
    """Helper function to get a valid expiration date for testing."""
    if target_date:
        return target_date
    return (date.today() + timedelta(days=90)).strftime("%Y-%m-%d")
