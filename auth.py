import os
from tastytrade import Session, Account
from dotenv import load_dotenv

load_dotenv()

username = os.getenv("TASTYTRADE_USERNAME_OR_EMAIL")
password = os.getenv("TASTYTRADE_PASSWORD")
account_id=os.getenv("TASTYTRADE_ACCOUNT_ID")

if not username or not password:
    raise ValueError("Missing credentials")

try:
    session = Session(username, password)
except Exception as e:
    raise ValueError(f"Failed to create new session: {str(e)}")

if account_id:
    account = Account.get_account(session, account_id)
else:
    account = Account.get_accounts(session)[0]