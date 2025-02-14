import keyring
from tastytrade import Session, Account
from mcp.server.fastmcp import FastMCP

# Initialize MCP server
mcp = FastMCP("TastyTrade")

username = keyring.get_password("tastytrade", "username")
password = keyring.get_password("tastytrade", "password")
account_id = keyring.get_password("tastytrade", "account_id")

if not username or not password:
    raise ValueError("Missing Tastytrade credentials in keyring. Use keyring.set_password() to set them.")

session = Session(username, password)
if not session:
    raise ValueError("Failed to create Tastytrade session.")

account = Account.get_account(session, account_id) if account_id else Account.get_accounts(session)[0]