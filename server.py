from typing import Literal

from mcp.server.fastmcp import FastMCP
import matplotlib.pyplot as plt

from src.core.order_manager import OrderManager
from src.core.order_logic import (
    load_and_review_queue,
    queue_order,
    execute_orders,
)
from src.tastytrade_api.auth import session, account
from src.tastytrade_api.functions import (
    get_balances,
    get_positions,
    get_transactions,
    get_market_metrics,
    get_bid_ask_price,
)

mcp = FastMCP("TastyTrade")
order_manager = OrderManager()

@mcp.tool()
def plot_nlv_history(
    time_back: Literal['1d', '1m', '3m', '6m', '1y', 'all'] = '1y'
) -> None:
    """Plots account net liquidating value history over time and displays it to the user.

    Args:
        time_back: Time period to plot. Options: '1d', '1m', '3m', '6m', '1y', 'all'
    """
    # Get historical data
    history = account.get_net_liquidating_value_history(session, time_back=time_back)

    # Create the plot
    plt.figure(figsize=(10, 6))
    plt.plot([n.time for n in history], [n.close for n in history], 'b-')

    # Customize the plot
    plt.title(f'Portfolio Value History (Past {time_back})')
    plt.xlabel('Date')
    plt.ylabel('Portfolio Value ($)')
    plt.grid(True)

    # Display the plot
    plt.show()

@mcp.tool()
async def queue_order_tool(
    symbol: str,
    quantity: int,
    action: Literal["Buy to Open", "Sell to Close"],
    execution_group: int = 1,
    dry_run: bool = False
) -> str:
    """Queue a new order for later batch execution.

    Args:
        symbol: Trading symbol
                Format examples:
                - Stocks: "AAPL", "SPY"
                - Options: "INTC 50C 2026-01-16"
        quantity: Number of shares/contracts to trade
        action: Order direction
                - "Buy to Open": Open a new long position
                - "Sell to Close": Close an existing long position
        execution_group: Batch execution group number (default: 1)
                        Orders in the same group are executed simultaneously.
                        Orders in different groups execute sequentially.
                        Use different groups when orders depend on each other
                        (e.g., selling one position to free up capital for another).
        dry_run: If True, simulates the order without actual execution (default: False)

    Returns:
        str: Confirmation message or error details
    """
    order_details = {
        "symbol": symbol,
        "quantity": quantity,
        "action": action,
        "execution_group": execution_group,
        "dry_run": dry_run,
    }
    try:
        result = await queue_order(order_manager, order_details)
        return f"Order queued successfully: {result}"
    except Exception as e:
        return f"Error queueing order: {str(e)}"

@mcp.tool()
async def review_queue_tool() -> str:
    """Review all currently queued orders.

    Returns:
        Formatted string showing all queued orders
    """
    tasks = load_and_review_queue(order_manager)
    if not tasks:
        return "Order queue is empty."

    # Convert the tasks into text for the user:
    output = ["Current Order Queue:", ""]
    output.append(f"{'Group':<6} {'Symbol':<20} {'Quantity':<10} {'Action':<15} {'Dry Run':<8}")
    output.append("-" * 60)
    for t in tasks:
        output.append(
            f"{t['group']:<6} {t['symbol']:<20} {t['quantity']:<10} "
            f"{t['action']:<15} {'Yes' if t['dry_run'] else 'No':<8}"
        )
    return "\n".join(output)

@mcp.tool()
async def execute_orders_tool(force: bool = False) -> str:
    """Execute all queued orders.

    Args:
        force: If True, execute even when market is closed (default: False)

    Returns:
        Execution status message
    """
    return await execute_orders(order_manager, force=force)

@mcp.tool()
async def cancel_orders_tool(
    execution_group: int | None = None,
    symbol: str | None = None,
) -> str:
    """Cancel queued orders based on provided filters.

    Args:
        execution_group: Group number to filter cancellations. 
                        If None, cancels orders across all groups.
        symbol: Symbol to filter cancellations.
                If None, cancels orders for all symbols.
                Format examples:
                - Stocks: "SPY", "AAPL"
                - Options: "SPY 150C 2025-01-19"

    Returns:
        str: Message describing which orders were cancelled.
    """
    try:
        result = order_manager.cancel_queued_orders(
            execution_group=execution_group,
            symbol=symbol
        )
        return result
    except Exception as e:
        return f"Error cancelling orders: {str(e)}"

@mcp.tool()
async def get_account_balances() -> str:
    """Get current account balances including cash balance, buying power, and net liquidating value.

    Returns:
        Formatted string showing account balance information
    """
    try:
        balances = await get_balances(session, account)
        return (
            f"Account Balances:\n"
            f"Cash Balance: ${balances.cash_balance:,.2f}\n"
            f"Buying Power: ${balances.buying_power:,.2f}\n"
            f"Net Liquidating Value: ${balances.net_liquidating_value:,.2f}\n"
            f"Maintenance Excess: ${balances.maintenance_excess:,.2f}"
        )
    except Exception as e:
        return f"Error fetching balances: {str(e)}"

@mcp.tool()
async def get_open_positions() -> str:
    """Get all currently open positions in the account.

    Returns:
        Formatted string showing all open positions
    """
    try:
        positions = await get_positions(session, account)
        if not positions:
            return "No open positions found."

        output = ["Current Positions:", ""]
        output.append(f"{'Symbol':<15} {'Type':<10} {'Quantity':<10} {'Value':<15}")
        output.append("-" * 50)

        for pos in positions:
            output.append(
                f"{pos.symbol:<15} {pos.instrument_type:<10} "
                f"{pos.quantity:<10} ${pos.value:,.2f}"
            )
        return "\n".join(output)
    except Exception as e:
        return f"Error fetching positions: {str(e)}"

@mcp.tool()
def get_transaction_history(start_date: str | None = None) -> str:
    """Get transaction history starting from a specific date.

    Args:
        start_date: Optional start date in YYYY-MM-DD format. If not provided, defaults to 90 days ago.

    Returns:
        Formatted string showing transaction history
    """
    try:
        transactions = get_transactions(session, account, start_date)
        if not transactions:
            return "No transactions found for the specified period."

        output = ["Transaction History:", ""]
        output.append(f"{'Date':<12} {'Sub Type':<15} {'Description':<45} {'Value':<15}")
        output.append("-" * 90)

        for txn in transactions:
            # Format the date
            date_str = txn.transaction_date.strftime("%Y-%m-%d")

            # Use transaction_sub_type for more clarity
            sub_type = txn.transaction_sub_type or 'N/A'

            # Use description for more detailed info
            description = txn.description or 'N/A'

            # Format value with dollar sign
            value = f"${float(txn.net_value):,.2f}" if txn.net_value is not None else 'N/A'

            output.append(
                f"{date_str:<12} {sub_type:<15} {description:<45} {value:<15}"
            )
        return "\n".join(output)
    except Exception as e:
        return f"Error fetching transactions: {str(e)}"

@mcp.tool()
async def get_metrics(symbols: list[str]) -> str:
    """Get market metrics for specified symbols including IV rank, liquidity, beta, etc.

    Args:
        symbols: List of stock symbols to get metrics for (e.g., ["SPY", "AAPL"])

    Returns:
        Formatted string showing market metrics for each symbol
    """
    try:
        metrics = await get_market_metrics(session, symbols)
        if not metrics:
            return "No metrics found for the specified symbols."

        output = ["Market Metrics:", ""]
        output.append(f"{'Symbol':<6} {'IV Rank':<8} {'IV %ile':<8} {'Beta':<6} {'Liquidity':<10}")
        output.append("-" * 45)

        for m in metrics:
            iv_rank = f"{float(m.implied_volatility_index_rank):.1f}%" if m.implied_volatility_index_rank else "N/A"
            iv_percentile = f"{float(m.implied_volatility_percentile):.1f}%" if m.implied_volatility_percentile else "N/A"

            output.append(
                f"{m.symbol:<6} {iv_rank:<8} {iv_percentile:<8} "
                f"{m.beta or 'N/A':<6} {m.liquidity_rating or 'N/A':<10}"
            )

            # Add earnings info if available
            if m.earnings:
                output.append(f"  Next Earnings: {m.earnings.expected_report_date} ({m.earnings.time_of_day})")

        return "\n".join(output)
    except Exception as e:
        return f"Error fetching market metrics: {str(e)}"

@mcp.tool()
async def get_prices(symbol: str) -> str:
    """Get current bid and ask prices for a stock or option.

    Args:
        symbol: Stock symbol (e.g., "SPY") or option description (e.g., "SPY 150C 2025-01-19")

    Returns:
        Formatted string showing bid and ask prices
    """
    try:
        bid, ask = await get_bid_ask_price(session, symbol)
        instrument_type = "Option" if " " in symbol else "Stock"
        return (
            f"{instrument_type} Prices for {symbol}:\n"
            f"Bid: ${float(bid):.2f}\n"
            f"Ask: ${float(ask):.2f}\n"
        )
    except Exception as e:
        return f"Error fetching prices: {str(e)}"