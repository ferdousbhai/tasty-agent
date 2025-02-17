import pytest
import pytest_asyncio
from datetime import datetime, timedelta
from uuid import UUID
from typing import AsyncGenerator
import warnings
import traceback
import asyncio

from .server import (
    session, account,  # noqa: F401 - used indirectly by imported functions
    schedule_trade,
    list_scheduled_trades,
    remove_scheduled_trade,
    plot_nlv_history,
    get_account_balances,
    get_open_positions,
    get_transaction_history,
    get_metrics
)


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

class TestTradeScheduling:
    @pytest.mark.asyncio
    async def test_schedule_stock_trade(self) -> None:
        """Test scheduling a stock trade."""
        result = await schedule_trade(
            action="Buy to Open",
            quantity=1,
            underlying_symbol="SPY",
            dry_run=True
        )
        assert "scheduled successfully" in result.lower()
        task_id = result.split()[1]
        assert UUID(task_id)  # Verify task_id is a valid UUID

    @pytest.mark.asyncio
    async def test_schedule_option_trade(self) -> None:
        """Test scheduling an option trade."""
        # Get the next monthly expiration (3rd Friday of next month)
        today = datetime.now().date()
        next_month = today.replace(day=1) + timedelta(days=32)  # Jump to next month
        next_month = next_month.replace(day=1)  # First of next month

        # Find the third Friday
        c = 0
        for i in range(1, 32):
            day = next_month + timedelta(days=i-1)
            if day.weekday() == 4:  # Friday
                c += 1
                if c == 3:  # Third Friday
                    expiration = day
                    break

        expiration_str = expiration.strftime("%Y-%m-%d")
        result = await schedule_trade(
            action="Buy to Open",
            quantity=1,
            underlying_symbol="SPY",
            strike=400,
            option_type="P",
            expiration_date=expiration_str,
            dry_run=True
        )
        assert "scheduled successfully" in result.lower()

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
    try:
        result = await get_open_positions()
        assert isinstance(result, str)
        assert "Current Positions:" in result or "No open positions found" in result
    except Exception as e:
        pytest.fail(f"Test failed with error: {str(e)}")

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

    @pytest.mark.asyncio
    async def test_get_metrics_with_empty_list(self) -> None:
        """Test getting metrics with empty symbol list."""
        # Create new event loop for this test
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            result = await get_metrics([])
            assert "No metrics found" in result
        except Exception as e:
            pytest.fail(f"Test failed with error: {str(e)}")
        finally:
            # Clean up
            loop.close()

@pytest.mark.asyncio
async def test_schedule_trade_invalid_time() -> None:
    """Test scheduling a trade with invalid time format."""
    result = await schedule_trade(
        action="Buy to Open",
        quantity=1,
        underlying_symbol="SPY",
        execution_type="once",
        run_time="25:00",  # Invalid time
        dry_run=True
    )
    assert "Invalid time format" in result.lower()

@pytest.mark.asyncio
async def test_schedule_trade_missing_runtime() -> None:
    """Test scheduling a trade without required run_time."""
    result = await schedule_trade(
        action="Buy to Open",
        quantity=1,
        underlying_symbol="SPY",
        execution_type="once",  # Requires run_time
        dry_run=True
    )
    assert "run_time parameter is required" in result.lower()