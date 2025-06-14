import sys
import keyring
from getpass import getpass
import click


@click.group(invoke_without_command=True)
@click.pass_context
def main(ctx):
    """TastyTrade MCP Server - Trade with your broker through AI."""
    if ctx.invoked_subcommand is None:
        from .server import mcp
        mcp.run()


@main.command()
def setup():
    """Set up TastyTrade credentials interactively."""
    from rich.console import Console
    from rich.prompt import Prompt, IntPrompt
    from rich.table import Table
    from tastytrade import Session, Account

    console = Console()
    console.print("[bold]Setting up Tastytrade credentials[/bold]")
    console.print("=" * 35)

    username = Prompt.ask("Enter your Tastytrade username")
    password = getpass("Enter your Tastytrade password: ")

    try:
        keyring.set_password("tastytrade", "username", username)
        keyring.set_password("tastytrade", "password", password)

        session = Session(username, password)
        accounts = Account.get(session)

        if len(accounts) > 1:
            table = Table(title="Available Accounts")
            table.add_column("Index", justify="right", style="cyan")
            table.add_column("Account Number", style="green")
            table.add_column("Name", style="blue")

            for idx, account in enumerate(accounts, 1):
                table.add_row(
                    str(idx),
                    account.account_number,
                    getattr(account, 'nickname', 'Main Account')
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
        for key in ["username", "password", "account_id"]:
            try:
                keyring.delete_password("tastytrade", key)
            except keyring.errors.PasswordDeleteError:
                pass
        sys.exit(1)