from lib.session import get_tasty_session
from lib.account import get_account

def get_positions():
    session = get_tasty_session()
    account = get_account(session)
    positions = account.get_positions(session)
    return positions

