from typing import Literal
import io
import base64
import matplotlib
import matplotlib.pyplot as plt
from zoneinfo import ZoneInfo

from mcp.server.fastmcp import FastMCP
from ..tastytrade_api.auth import session, account
from ..tastytrade_api.functions import (
    get_balances, get_positions, get_transactions,
    get_market_metrics, get_bid_ask_price,
)

# Initialize MCP and constants
mcp = FastMCP("TastyTrade")
NYC_TIMEZONE = ZoneInfo("America/New_York")

@mcp.tool()
def plot_nlv_history(
    time_back: Literal['1d', '1m', '3m', '6m', '1y', 'all'] = '1y'
) -> str:
    """Plot the account's net liquidating value history and return as a base64 PNG image.

    Args:
        time_back: Time period to plot. Options: '1d', '1m', '3m', '6m', '1y', 'all'

    Returns:
        str: Base64-encoded PNG image data of the generated plot
    """
    # Get historical data
    history = account.get_net_liquidating_value_history(session, time_back=time_back)

    # Use Agg backend for creating the base64 image
    matplotlib.use("Agg")

    # Create the plot
    fig, ax = plt.subplots(figsize=(10, 6))
    ax.plot([n.time for n in history], [n.close for n in history], 'b-')
    ax.set_title(f'Portfolio Value History (Past {time_back})')
    ax.set_xlabel('Date')
    ax.set_ylabel('Portfolio Value ($)')
    ax.grid(True)

    # Encode the figure in base64
    buffer = io.BytesIO()
    fig.savefig(buffer, format='png')
    buffer.seek(0)
    base64_str = base64.b64encode(buffer.read()).decode('utf-8')

    # close the figure to free resources
    plt.close(fig)

    return base64_str

# Market Data Tools
@mcp.tool()
async def get_account_balances() -> str:
    """Get current account balances and buying power information.

    Returns:
        str: Formatted string containing:
            - Cash Balance
            - Buying Power
            - Net Liquidating Value
            - Maintenance Excess
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
    """Get all currently open positions in the trading account.

    Returns:
        str: Formatted table showing Symbol, Position Type, Quantity, and Current Value
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
    """Get detailed transaction history for the account.

    Args:
        start_date: Optional start date in YYYY-MM-DD format (e.g., '2024-01-01').
            If not provided, returns last 90 days of transactions.

    Returns:
        str: Formatted table showing Transaction Date, Transaction Type, Description, and Value
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
    """Get market metrics and analysis for specified stock symbols.

    Args:
        symbols: List of stock symbols to analyze (e.g., ['AAPL', 'MSFT'])

    Returns:
        str: Formatted table showing IV Rank, IV Percentile, Beta, Liquidity Rating, and Next Earnings Date (if available)
    """
    try:
        metrics = await get_market_metrics(session, symbols)
        if not metrics:
            return "No metrics found for the specified symbols."

        output = ["Market Metrics:", ""]
        output.append(f"{'Symbol':<6} {'IV Rank':<8} {'IV %ile':<8} {'Beta':<6} {'Liquidity':<10}")
        output.append("-" * 45)

        for m in metrics:
            iv_rank = f"{float(m.implied_volatility_index_rank * 100):.1f}%" if m.implied_volatility_index_rank else "N/A"
            iv_percentile = f"{float(m.implied_volatility_percentile * 100):.1f}%" if m.implied_volatility_percentile else "N/A"
            beta = f"{float(m.beta):.2f}" if m.beta else "N/A"

            output.append(
                f"{m.symbol:<6} {iv_rank:<8} {iv_percentile:<8} "
                f"{beta:<6} {m.liquidity_rating or 'N/A':<10}"
            )

            if m.earnings:
                output.append(f"  Next Earnings: {m.earnings.expected_report_date} ({m.earnings.time_of_day})")

        return "\n".join(output)
    except Exception as e:
        return f"Error fetching market metrics: {str(e)}"

@mcp.tool()
async def get_prices(symbol: str) -> str:
    """Get current market prices for a stock or option.

    Args:
        symbol: Stock symbol (e.g., 'AAPL') or option symbol in format "SYMBOL STRIKE{C|P} YYYY-MM-DD"
            (e.g., "SPY 600C 2025-01-19" for SPY $600 Call expiring Jan 19, 2025)

    Returns:
        str: Current bid and ask prices
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