from lib.session import get_tasty_session
from lib.account import get_account

def get_balances():
    session = get_tasty_session()
    account = get_account(session)
    balance = account.get_balances(session)
    return balance

