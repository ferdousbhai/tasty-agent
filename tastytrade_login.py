import os
from dotenv import load_dotenv
from tastytrade import Session, Account

def get_session_and_account():
    """
    Create and return a Tastytrade session and the first available account.
    Raises ValueError if there is a problem.
    """
    load_dotenv()

    username = os.getenv("TASTYTRADE_USER")
    password = os.getenv("TASTYTRADE_PASSWORD")

    if not username or not password:
        raise ValueError("Missing TASTYTRADE_USER or TASTYTRADE_PASSWORD environment variables.")

    session = Session(username, password)
    if not session:
        raise ValueError("Failed to create Tastytrade session.")

    accounts = Account.get_accounts(session)
    if not accounts:
        raise ValueError("No valid accounts found.")

    return session, accounts[0]