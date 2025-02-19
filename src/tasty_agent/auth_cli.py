import keyring
from getpass import getpass
from rich.console import Console
from rich.prompt import Prompt, IntPrompt
from rich.table import Table
from tastytrade import Session, Account

console = Console()

def auth():
    """Interactive command-line setup for Tastytrade credentials."""
    console.print("[bold]Setting up Tastytrade credentials[/bold]")
    console.print("=" * 35)

    username = Prompt.ask("Enter your Tastytrade username")
    password = getpass("Enter your Tastytrade password: ")  # getpass hides the password while typing

    try:
        # Store initial credentials
        keyring.set_password("tastytrade", "username", username)
        keyring.set_password("tastytrade", "password", password)

        # Create session and get accounts
        session = Session(username, password)
        accounts = Account.get_accounts(session)

        if len(accounts) > 1:
            # Show account selection table
            table = Table(title="Available Accounts")
            table.add_column("Index", justify="right", style="cyan")
            table.add_column("Account Number", style="green")
            table.add_column("Name", style="blue")

            for idx, account in enumerate(accounts, 1):
                table.add_row(
                    str(idx),
                    account.account_number,
                    getattr(account, 'name', 'Main Account')  # Fallback if name not available
                )

            console.print(table)

            # Get user selection
            choice = IntPrompt.ask(
                "\nSelect account by index",
                choices=[str(i) for i in range(1, len(accounts) + 1)]
            )
            selected_account = accounts[choice - 1]
        else:
            selected_account = accounts[0]
            console.print(f"\nSingle account found: [green]{selected_account.account_number}[/green]")

        # Store selected account ID
        keyring.set_password("tastytrade", "account_id", selected_account.account_number)

        console.print("\n[bold green]✓[/bold green] Credentials verified successfully!")
        console.print(f"Connected to account: [green]{selected_account.account_number}[/green]")

    except Exception as e:
        console.print(f"\n[bold red]Error setting up credentials:[/bold red] {str(e)}")
        # Clean up on failure
        for key in ["username", "password", "account_id"]:
            try:
                keyring.delete_password("tastytrade", key)
            except keyring.errors.PasswordDeleteError:
                pass
        return False
    return True
