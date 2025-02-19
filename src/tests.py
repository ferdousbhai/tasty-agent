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
import exchange_calendars as xcals
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
        expiration_str = get_expiration_date()
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
    assert "invalid time format" in result.lower()

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

# Add new test class for get_prices
class TestPrices:
    @pytest.mark.asyncio
    async def test_get_stock_prices(self) -> None:
        """Test getting prices for a stock."""
        result = await get_prices(underlying_symbol="SPY")
        assert "Current prices for SPY" in result
        assert "Bid: $" in result
        assert "Ask: $" in result

    @pytest.mark.asyncio
    async def test_get_option_prices(self) -> None:
        """Test getting prices for an option."""
        expiration_str = get_expiration_date()
        result = await get_prices(
            underlying_symbol="SPY",
            expiration_date=expiration_str,
            option_type="P",
            strike=400
        )
        assert "Current prices for" in result
        assert "Bid: $" in result
        assert "Ask: $" in result

    @pytest.mark.asyncio
    async def test_get_prices_invalid_symbol(self) -> None:
        """Test getting prices for an invalid symbol."""
        result = await get_prices(underlying_symbol="INVALID")
        assert "Could not find instrument" in result

    @pytest.mark.asyncio
    async def test_get_prices_invalid_date(self) -> None:
        """Test getting prices with invalid date format."""
        result = await get_prices(
            underlying_symbol="SPY",
            expiration_date="invalid-date",
            option_type="P",
            strike=400
        )
        assert "Invalid expiration date format" in result

    @pytest.mark.asyncio
    async def test_get_prices_missing_option_params(self) -> None:
        """Test getting prices with incomplete option parameters."""
        result = await get_prices(
            underlying_symbol="SPY",
            option_type="P",
        )
        assert "Could not find instrument" in result

class TestExpirationDate:
    def test_get_expiration_date_default(self) -> None:
        """Test getting default expiration date (90+ days out)."""
        today = date.today()
        expiry = get_expiration_date()
        expiry_date = datetime.strptime(expiry, "%Y-%m-%d").date()

        # Verify it's a Friday
        assert expiry_date.weekday() == 4

        # Verify it's at least 90 days out
        days_out = (expiry_date - today).days
        assert days_out >= 90

        # Verify it's the third Friday
        month_start = expiry_date.replace(day=1)
        friday_count = sum(1 for i in range(expiry_date.day) if (month_start + timedelta(days=i)).weekday() == 4)
        assert friday_count == 3

    def test_get_expiration_date_with_target(self) -> None:
        """Test getting expiration date after specific target date."""
        target = (date.today() + timedelta(days=45)).strftime("%Y-%m-%d")
        expiry = get_expiration_date(target)
        expiry_date = datetime.strptime(expiry, "%Y-%m-%d").date()
        target_date = datetime.strptime(target, "%Y-%m-%d").date()

        # Verify it's after target date
        assert expiry_date > target_date

        # Verify it's a monthly expiration (third Friday)
        assert expiry_date.weekday() == 4
        month_start = expiry_date.replace(day=1)
        friday_count = sum(1 for i in range(expiry_date.day) if (month_start + timedelta(days=i)).weekday() == 4)
        assert friday_count == 3

    def test_get_expiration_date_invalid_format(self) -> None:
        """Test with invalid date format."""
        result = get_expiration_date("2024/01/01")
        assert "Invalid date format" in result


def get_expiration_date(target_date: str | None = None) -> str:
    """Get the next monthly option expiration date after the target date.
    If no target date is provided, returns the first monthly expiration ≥90 days out.
    """
    # Get CBOE calendar
    cboe = xcals.get_calendar("XCBOE")
    try:
        search_date = (datetime.strptime(target_date, "%Y-%m-%d").date() if target_date else date.today() + timedelta(days=90))
        # Start from first day of month and find third Friday
        current = search_date.replace(day=1)
        while current.weekday() != 4:  # Move to first Friday
            current += timedelta(days=1)
        current += timedelta(weeks=2)  # Move to third Friday

        # Move to next month if needed
        if current < search_date:
            current = (current + timedelta(days=32)).replace(day=1)
            while current.weekday() != 4:
                current += timedelta(days=1)
            current += timedelta(weeks=2)

        # Get previous trading day if not a valid session
        return cboe.date_to_session(current, direction="previous").strftime("%Y-%m-%d")

    except ValueError:
        return "Invalid date format. Please use YYYY-MM-DD format"
