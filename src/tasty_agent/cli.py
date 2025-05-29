import sys
import keyring
from getpass import getpass

def auth() -> bool:
    """Interactive command-line setup for Tastytrade credentials."""
    from rich.console import Console
    from rich.prompt import Prompt, IntPrompt
    from rich.table import Table
    from tastytrade import Session, Account

    console = Console()
    console.print("[bold]Setting up Tastytrade credentials[/bold]")
    console.print("=" * 35)

    username = Prompt.ask("Enter your Tastytrade username")
    password = getpass("Enter your Tastytrade password: ")  # getpass hides the password while typing

    try:
        keyring.set_password("tastytrade", "username", username)
        keyring.set_password("tastytrade", "password", password)

        session = Session(username, password)
        accounts = Account.get(session)

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
                    getattr(account, 'nickname', 'Main Account')  # Use nickname instead of name
                )

            console.print(table)

            choice = IntPrompt.ask(
                "\nSelect account by index",
                choices=[str(i) for i in range(1, len(accounts) + 1)]
            )
            selected_account = accounts[choice - 1]
        else:
            selected_account = accounts[0]
            console.print(f"\nSingle account found: [green]{selected_account.account_number}[/green]")

        keyring.set_password("tastytrade", "account_id", selected_account.account_number)

        console.print("\n[bold green]✓[/bold green] Credentials verified successfully!")
        console.print(f"Connected to account: [green]{selected_account.account_number}[/green]")

    except Exception as e:
        console.print(f"\n[bold red]Error setting up credentials:[/bold red] {str(e)}")
        # Clean up
        for key in ["username", "password", "account_id"]:
            try:
                keyring.delete_password("tastytrade", key)
            except keyring.errors.PasswordDeleteError:
                pass
        return False
    return True


def main():
    """Main entry point for the tasty-agent CLI."""
    if len(sys.argv) > 1 and sys.argv[1] == "setup":
        if auth():
            print("Authentication successful.", file=sys.stdout)
        else:
            print("Authentication failed.", file=sys.stderr)
        sys.exit(0) # Exit
    else:
        # Run the MCP server
        try:
            from .server import mcp
        except ImportError:
            print("Error: Could not import server module.", file=sys.stderr)
            sys.exit(1)

        mcp.run()

if __name__ == "__main__":
    main()