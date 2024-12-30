import os
import logging
from tastytrade import Account

logging.basicConfig(level=logging.INFO)

def get_account(session, account_id=os.getenv("TASTYTRADE_ACCOUNT_ID")):
    try:
        if account_id:
            logging.info(f"Fetching account with ID: {account_id}")
            return Account.get_account(session, account_id)
    except Exception as e:
        logging.warning(f"Failed to get account {account_id}: {e}")

    logging.info("Falling back to first available account")
    return Account.get_accounts(session)[0]