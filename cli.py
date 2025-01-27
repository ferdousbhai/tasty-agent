import logging
import asyncio
from decimal import Decimal, InvalidOperation

from rich.prompt import Prompt, Confirm
from rich.table import Table
from rich.console import Console
from rich.panel import Panel
from rich import print as rprint

from src.core.order_manager import OrderManager
from src.core.utils import is_market_open, get_time_until_market_open
from src.core.order_logic import (
    load_and_review_queue,
    queue_order,
    execute_orders,
)
from src.tastytrade_api.auth import session, account
from src.tastytrade_api.functions import (
    get_balances,
    get_positions,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Create a single instance to manage orders
manager = OrderManager()

def prompt_user_for_order():
    """
    Prompt the user for order details and return them as a dictionary.
    """
    # Symbol validation
    while True:
        symbol = input("Enter symbol (e.g., AAPL or INTC 50C 2026-01-16): ").strip().upper()

        # Stock ticker validation (simple format check)
        if len(symbol.split()) == 1:
            if symbol.isalpha() and 1 <= len(symbol) <= 5:
                break
        # Option format validation
        elif len(symbol.split()) == 3:
            try:
                ticker, strike_price, expiry = symbol.split()
                if not ticker.isalpha() or not 1 <= len(ticker) <= 5:
                    raise ValueError("Invalid ticker symbol")

                # Validate strike price format (number followed by P or C)
                strike = strike_price[:-1]
                option_type = strike_price[-1]
                float(strike)  # Will raise ValueError if not a number
                if option_type not in ['C', 'P']:
                    raise ValueError("Option type must be C or P")

                # Basic date format validation (YYYY-MM-DD)
                year, month, day = expiry.split('-')
                if not (len(year) == 4 and len(month) == 2 and len(day) == 2):
                    raise ValueError("Invalid date format")
                break
            except (ValueError, IndexError):
                print("Invalid option format. Use format: 'TICKER STRIKEP/C YYYY-MM-DD'")
        else:
            print("Invalid symbol format. Use stock ticker (e.g., AAPL) or option format (e.g., INTC 50C 2026-01-16)")

    # Quantity validation
    while True:
        qty_str = input("Enter quantity (e.g. 100): ").strip()
        try:
            quantity = int(qty_str)
            if 1 <= quantity <= 5000:
                break
            else:
                print("Quantity must be between 1 and 5000.")
        except ValueError:
            print("Invalid quantity. Please enter a numeric value.")

    # Action
    valid_actions = ["Buy to Open", "Sell to Close"]
    action = Prompt.ask(
        "Enter action",
        choices=valid_actions,
        default=valid_actions[0],
    )

    # Execution Group
    group_input = input("Enter execution group (default=1): ").strip()
    group = 1
    if group_input:
        try:
            group = int(Decimal(group_input))
        except (ValueError, TypeError, InvalidOperation):
            print("Invalid group input; using default = 1.")

    # Dry run?
    dry_run = Confirm.ask("Dry run?", default=False)

    return {
        "symbol": symbol,
        "quantity": quantity,
        "action": action,
        "execution_group": group,
        "dry_run": dry_run,
    }


async def queue_order_flow():
    """
    Ask the user for details of an order and queue it (async).
    """
    console = Console()
    order_details = prompt_user_for_order()
    try:
        # Call the manager's queue_order method
        msg = await queue_order(manager, order_details)
        console.print(f"[bold green]Order queued successfully:[/bold green] {msg}")
    except Exception as e:
        console.print(f"[bold red]Error queueing order:[/bold red] {str(e)}")


async def review_queue_flow():
    """
    Load and review the current queue (async only for consistency).
    """
    console = Console()
    tasks = load_and_review_queue(manager)
    if not tasks:
        console.print("[bold red]Order queue is empty.[/bold red]")
        return

    # Create a Table
    table = Table(title="Current Order Queue", show_edge=True, header_style="bold magenta", box=None)
    table.add_column("Group", justify="center", style="cyan", no_wrap=True)
    table.add_column("Symbol", justify="left", style="white", no_wrap=True)
    table.add_column("Quantity", justify="right", style="white", no_wrap=True)
    table.add_column("Action", justify="left", style="white", no_wrap=True)
    table.add_column("Dry Run?", justify="center", style="white", no_wrap=True)

    # Populate the table
    for t in tasks:
        table.add_row(
            str(t["group"]),
            t["symbol"],
            str(t["quantity"]),
            t["action"],
            "Yes" if t["dry_run"] else "No",
        )

    console.print(table)


async def execute_orders_flow():
    """
    Execute all the queued tasks asynchronously. Wait for the market to open if
    there are tasks in the queue that are not dry_run.
    """
    console = Console()

    # Load tasks and provide feedback
    tasks = load_and_review_queue(manager)
    if not tasks:
        console.print("[yellow]No orders in queue to execute.[/yellow]")
        return

    # Market open check
    non_dry_run_tasks = [t for t in tasks if not t.get("dry_run", False)]
    if non_dry_run_tasks and not is_market_open():
        console.print("[yellow]Market is closed. Waiting for market to open...[/yellow]")
        with console.status("[yellow]Time until market open: ", spinner="dots") as status:
            try:
                while not is_market_open():
                    delta = get_time_until_market_open()
                    hours, remainder = divmod(int(delta.total_seconds()), 3600)
                    minutes, seconds = divmod(remainder, 60)
                    status.update(f"[yellow]Time until market open: {hours:02d}h {minutes:02d}m {seconds:02d}s")
                    await asyncio.sleep(1)  # Update every second instead of every minute
            except asyncio.TimeoutError:
                console.print("[bold red]Timeout waiting for market to open.[/bold red]")
                return
        console.print("[bold green]Market is now open. Proceeding with execution...[/bold green]")

    # Execute orders
    try:
        with console.status("Executing orders...", spinner="dots"):
            result = await execute_orders(manager)
        console.print(f"[bold green]{result}[/bold green]")
    except Exception as e:
        console.print(f"[bold red]Error executing tasks:[/bold red] {str(e)}")


async def cancel_orders_flow():
    """
    Cancel orders based on user input filters.
    """
    console = Console()

    # First show current queue
    await review_queue_flow()

    # Get filter criteria
    filter_type = Prompt.ask(
        "Cancel by",
        choices=["group", "symbol", "all"],
        default="all"
    )

    try:
        if filter_type == "group":
            group_input = input("Enter execution group number: ").strip()
            try:
                group = int(group_input)
                symbol_filter = Confirm.ask("Filter by symbol also?", default=False)
                symbol = input("Enter symbol: ").strip() if symbol_filter else None
                result = manager.cancel_queued_orders(execution_group=group, symbol=symbol)
            except ValueError:
                raise ValueError("Invalid group number")

        elif filter_type == "symbol":
            symbol = input("Enter symbol: ").strip()
            result = manager.cancel_queued_orders(symbol=symbol)

        else:  # "all"
            if Confirm.ask("Are you sure you want to cancel ALL orders?", default=False):
                result = manager.cancel_queued_orders()
            else:
                console.print("[yellow]Cancellation aborted.[/yellow]")
                return

        console.print(f"[bold green]{result}[/bold green]")

    except Exception as e:
        console.print(f"[bold red]Error cancelling orders:[/bold red] {str(e)}")


async def get_balances_flow():
    """
    Display current account balances.
    """
    console = Console()
    try:
        with console.status("Fetching account balances...", spinner="dots"):
            balances = await get_balances(session, account)
        
        # Create a table for better visualization
        table = Table(title="Account Balances", show_edge=True, header_style="bold magenta", box=None)
        table.add_column("Metric", style="cyan")
        table.add_column("Value", justify="right", style="green")
        
        table.add_row("Cash Balance", f"${balances.cash_balance:,.2f}")
        table.add_row("Buying Power", f"${balances.buying_power:,.2f}")
        table.add_row("Net Liquidating Value", f"${balances.net_liquidating_value:,.2f}")
        table.add_row("Maintenance Excess", f"${balances.maintenance_excess:,.2f}")
        
        console.print(table)
    except Exception as e:
        console.print(f"[bold red]Error fetching balances:[/bold red] {str(e)}")

async def get_positions_flow():
    """
    Display current open positions.
    """
    console = Console()
    try:
        with console.status("Fetching open positions...", spinner="dots"):
            positions = await get_positions(session, account)
        
        if not positions:
            console.print("[yellow]No open positions found.[/yellow]")
            return
            
        # Create a table for positions
        table = Table(title="Open Positions", show_edge=True, header_style="bold magenta", box=None)
        table.add_column("Symbol", style="cyan")
        table.add_column("Type", style="white")
        table.add_column("Quantity", justify="right", style="white")
        table.add_column("Value", justify="right", style="green")
        
        for pos in positions:
            table.add_row(
                pos.symbol,
                pos.instrument_type,
                str(pos.quantity),
                f"${pos.value:,.2f}"
            )
            
        console.print(table)
    except Exception as e:
        console.print(f"[bold red]Error fetching positions:[/bold red] {str(e)}")

async def cli():
    """
    Main CLI loop. Using Prompt.ask with a list of choices.
    """
    while True:
        menu_str = (
            "[bold magenta]--- Tastytrade Order Manager CLI ---[/bold magenta]\n"
            " [bold green]queue[/bold green]     :  Queue an order\n"
            " [bold green]review[/bold green]    :  Review orders\n"
            " [bold green]execute[/bold green]   :  Execute all queued orders\n"
            " [bold green]cancel[/bold green]    :  Cancel queued orders\n"
            " [bold green]balances[/bold green]  :  Show account balances\n"
            " [bold green]positions[/bold green] :  Show open positions\n"
            " [bold green]quit[/bold green]      :  Exit the CLI\n"
        )
        rprint(Panel(menu_str, border_style="magenta", title="Main Menu", expand=False))

        choice = Prompt.ask(
            "Select an option",
            choices=["queue", "review", "execute", "cancel", "balances", "positions", "exit"],
            default="queue",
        )

        if choice == "queue":
            await queue_order_flow()
        elif choice == "review":
            await review_queue_flow()
        elif choice == "execute":
            await execute_orders_flow()
        elif choice == "cancel":
            await cancel_orders_flow()
        elif choice == "balances":
            await get_balances_flow()
        elif choice == "positions":
            await get_positions_flow()
        elif choice in ("exit", "quit"):
            print("Exiting CLI. Goodbye.")
            break
        else:
            print("Invalid choice. Please select from the provided list.")


def run():
    asyncio.run(cli())