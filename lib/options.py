from lib.session import get_tasty_session
from tastytrade.instruments import get_option_chain
from tastytrade.utils import get_tasty_monthly
from dotenv import load_dotenv

load_dotenv()
session = get_tasty_session()
chain = get_option_chain(session, 'SPLG')
exp = get_tasty_monthly()  # 45 DTE expiration!
print(chain[exp][0])