from datetime import date, datetime, timedelta
from typing import Literal

import matplotlib.pyplot as plt

from lib.session import get_tasty_session
from lib.account import get_account


def get_transactions(start_date=None):
    """Get transaction history starting from a specific date.

    Args:
        start_date (str, optional): Date string in YYYY-MM-DD format (e.g., "2024-01-01")
        If not provided, defaults to 90 days ago
    """
    if start_date is None:
        # Default to 90 days ago
        date_obj = date.today() - timedelta(days=90)
    else:
        # Convert string date to date object
        try:
            date_obj = datetime.strptime(start_date, "%Y-%m-%d").date()
        except ValueError:
            raise ValueError("start_date must be in YYYY-MM-DD format (e.g., '2024-01-01')")

    session = get_tasty_session()
    account = get_account(session)
    history = account.get_history(session, start_date=date_obj)

    return history


def plot_pl_history(
    time_back: Literal['1d', '1m', '3m', '6m', '1y', 'all'] = '1m',
    save_path: str | None = None
) -> None:
    """Plot profit/loss history over time by showing net liquidating value changes.

    Args:
        time_back: Time period to plot. Options: '1d', '1m', '3m', '6m', '1y', 'all'
        save_path: Optional path to save the plot (e.g., 'pl_history.png')
        If None, displays the plot interactively
    """
    session = get_tasty_session()
    account = get_account(session)

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

    # Either save or display the plot
    if save_path:
        plt.savefig(save_path)
        plt.close()
    else:
        plt.show()


if __name__ == "__main__":
    plot_pl_history(time_back="1y", save_path="portfolio_history.png")

