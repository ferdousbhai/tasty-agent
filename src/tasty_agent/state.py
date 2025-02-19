import keyring
import logging
from datetime import datetime, timedelta
from typing import Self

from tastytrade import Session, Account
from tastytrade.account import AccountBalance, CurrentPosition

logger = logging.getLogger(__name__)

class AccountState:
    def __init__(self) -> None:
        # Session management
        self._session: Session | None = None
        self._account: Account | None = None
        self._last_session_refresh: datetime | None = None
        self._session_refresh_interval = timedelta(hours=23)

        # Credentials
        self.username = keyring.get_password("tastytrade", "username")
        self.password = keyring.get_password("tastytrade", "password")
        self.account_id = keyring.get_password("tastytrade", "account_id")

        if not self.username or not self.password:
            raise ValueError("Missing Tastytrade credentials in keyring. Use keyring.set_password() to set them.")

        # State variables with timestamps
        self._positions: list[CurrentPosition] | None = None
        self._last_positions_refresh: datetime | None = None

        self._balances: AccountBalance | None = None
        self._last_balances_refresh: datetime | None = None

        # Default refresh intervals
        self._positions_refresh_interval = timedelta(minutes=5)
        self._balances_refresh_interval = timedelta(minutes=5)

    def _needs_session_refresh(self) -> bool:
        if not self._last_session_refresh:
            return True
        return datetime.now() - self._last_session_refresh > self._session_refresh_interval

    def _create_session(self) -> None:
        self._session = Session(self.username, self.password)
        if not self._session:
            raise ValueError("Failed to create Tastytrade session.")

        self._account = (
            Account.get_account(self._session, self.account_id) 
            if self.account_id 
            else Account.get_accounts(self._session)[0]
        )
        self._last_session_refresh = datetime.now()

    @property
    def session(self) -> Session:
        if self._needs_session_refresh():
            self._create_session()
        return self._session

    @property
    def account(self) -> Account:
        if self._needs_session_refresh():
            self._create_session()
        return self._account

    async def get_positions(self, force_refresh: bool = False) -> list[CurrentPosition]:
        """Get current positions, refreshing if needed or forced."""
        now = datetime.now()
        needs_refresh = (
            force_refresh or
            self._positions is None or
            self._last_positions_refresh is None or
            now - self._last_positions_refresh > self._positions_refresh_interval
        )

        if needs_refresh:
            self._positions = await self.account.a_get_positions(self.session)
            self._last_positions_refresh = now
            logger.debug("Refreshed positions")

        return self._positions

    async def get_balances(self, force_refresh: bool = False) -> AccountBalance:
        """Get current balances, refreshing if needed or forced."""
        now = datetime.now()
        needs_refresh = (
            force_refresh or
            self._balances is None or
            self._last_balances_refresh is None or
            now - self._last_balances_refresh > self._balances_refresh_interval
        )

        if needs_refresh:
            self._balances = await self.account.a_get_balances(self.session)
            self._last_balances_refresh = now
            logger.debug("Refreshed balances")

        return self._balances

    def invalidate_positions(self) -> None:
        """Force positions to be refreshed on next get_positions() call."""
        self._last_positions_refresh = None
        self._positions = None

    def invalidate_balances(self) -> None:
        """Force balances to be refreshed on next get_balances() call."""
        self._last_balances_refresh = None
        self._balances = None

    # Singleton pattern
    _instance: Self | None = None

    @classmethod
    def get_instance(cls) -> Self:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

# Create global instance
account_state = AccountState.get_instance()