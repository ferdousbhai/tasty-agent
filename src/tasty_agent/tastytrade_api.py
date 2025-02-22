import asyncio
from datetime import datetime, timedelta, date
from decimal import Decimal
from typing import Literal, Self
import keyring
import logging

from tastytrade import Session, Account, metrics
from tastytrade.account import AccountBalance, CurrentPosition
from tastytrade.order import NewOrder, PlacedOrder, OrderStatus, OrderAction, OrderTimeInForce, OrderType, Leg
from tastytrade.instruments import Option, Equity, NestedOptionChain
from tastytrade.streamer import DXLinkStreamer
from tastytrade.dxfeed import Quote

logger = logging.getLogger(__name__)

class TastytradeAPI:
    def __init__(self) -> None:
        # Session management
        self._session: Session | None = None
        self._account: Account | None = None
        self._last_session_refresh: datetime | None = None
        self._session_refresh_interval = timedelta(hours=23)

        # State variables
        self._positions: list[CurrentPosition] | None = None
        self._balances: AccountBalance | None = None

        # Credentials
        self.username = keyring.get_password("tastytrade", "username")
        self.password = keyring.get_password("tastytrade", "password")
        self.account_id = keyring.get_password("tastytrade", "account_id")

        if not self.username or not self.password:
            raise ValueError("Missing Tastytrade credentials in keyring. Use keyring.set_password() to set them.")

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
        """Get current positions, refreshing only if forced or not yet loaded."""
        if force_refresh or self._positions is None:
            self._positions = await self.account.a_get_positions(self.session)
            logger.debug("Refreshed positions")

        return self._positions

    async def get_balances(self, force_refresh: bool = False) -> AccountBalance:
        """Get current account balances, refreshing only if forced or not yet loaded."""
        if force_refresh or self._balances is None:
            self._balances = await self.account.a_get_balances(self.session)
            logger.debug("Refreshed balances")

        return self._balances

    def invalidate_positions(self) -> None:
        """Force positions to be refreshed on next get_positions() call."""
        self._positions = None

    def invalidate_balances(self) -> None:
        """Force balances to be refreshed on next get_balances() call."""
        self._balances = None

    async def create_instrument(
        self,
        underlying_symbol: str,
        expiration_date: datetime | None = None,
        option_type: Literal["C", "P"] | None = None,
        strike: float | None = None,
    ) -> Option | Equity | None:
        """Create an instrument object for a given symbol."""
        try:
            # If no option parameters, treat as equity
            if not any([expiration_date, option_type, strike]):
                return Equity.get_equity(self.session, underlying_symbol)

            # Validate all option parameters are present
            if not all([expiration_date, option_type, strike]):
                logger.error("Must provide all option parameters (expiration_date, option_type, strike) or none")
                return None

            try:
                # Get option chain
                chain: list[NestedOptionChain] = NestedOptionChain.get_chain(self.session, underlying_symbol)

                if not chain:
                    logger.error(f"No option chain found for {underlying_symbol}")
                    return None

                option_chain = chain[0]

                # Find matching expiration
                exp_date = expiration_date.date()
                expiration = next(
                    (exp for exp in option_chain.expirations 
                    if exp.expiration_date == exp_date),
                    None
                )
                if not expiration:
                    logger.error(f"No expiration found for date {exp_date}")
                    return None

                # Find matching strike
                strike_obj = next(
                    (s for s in expiration.strikes 
                    if float(s.strike_price) == strike),
                    None
                )
                if not strike_obj:
                    logger.error(f"No strike found for {strike}")
                    return None

                # Get option symbol based on type
                option_symbol = strike_obj.call if option_type == "C" else strike_obj.put
                return Option.get_option(self.session, option_symbol)

            except Exception as e:
                logger.error(f"Error getting option chain: {e}")
                return None

        except Exception as e:
            logger.error(f"Error getting instrument for {underlying_symbol}: {e}")
            return None

    async def get_prices(
        self,
        underlying_symbol: str,
        expiration_date: str | None = None,
        option_type: Literal["C", "P"] | None = None,
        strike: float | None = None,
    ) -> tuple[Decimal, Decimal] | str:
        """Get current bid/ask prices for a stock or option."""
        try:
            # Convert expiration_date string to datetime if provided
            expiry_datetime = None
            if expiration_date:
                try:
                    expiry_datetime = datetime.strptime(expiration_date, "%Y-%m-%d")
                except ValueError:
                    return "Invalid expiration date format. Please use YYYY-MM-DD format"

            # Get instrument
            instrument = await self.create_instrument(
                underlying_symbol=underlying_symbol,
                expiration_date=expiry_datetime,
                option_type=option_type,
                strike=strike
            )
            if instrument is None:
                return f"Could not find instrument for symbol: {underlying_symbol}"

            # Get streamer symbol
            streamer_symbol = instrument.streamer_symbol
            if not streamer_symbol:
                return f"Could not get streamer symbol for {instrument.symbol}"

            return await self.get_quote(streamer_symbol)

        except Exception as e:
            return f"Error fetching prices: {str(e)}"

    async def get_quote(self, streamer_symbol: str) -> tuple[Decimal, Decimal] | str:
        """Get current quote for a symbol."""
        try:
            async with DXLinkStreamer(self.session) as streamer:
                await streamer.subscribe(Quote, [streamer_symbol])
                quote = await asyncio.wait_for(streamer.get_event(Quote), timeout=10.0)
                return Decimal(str(quote.bid_price)), Decimal(str(quote.ask_price))
        except asyncio.TimeoutError:
            return f"Timed out waiting for quote data for {streamer_symbol}"
        except Exception as e:
            return f"Error getting quote: {str(e)}"

    def get_nlv_history(self, time_back: str) -> list:
        """Get net liquidating value history."""
        return self.account.get_net_liquidating_value_history(self.session, time_back=time_back)

    def get_transaction_history(self, start_date: date) -> list:
        """Get transaction history."""
        return self.account.get_history(self.session, start_date=start_date)

    async def get_market_metrics(self, symbols: list[str]):
        """Get market metrics for symbols."""
        return await metrics.a_get_market_metrics(self.session, symbols)

    def get_live_orders(self):
        """Get live orders."""
        return self.account.get_live_orders(self.session)

    def place_order(self, order: NewOrder, dry_run: bool = False) -> PlacedOrder:
        """Place a new order."""
        return self.account.place_order(self.session, order, dry_run=dry_run)

    def replace_order(self, order_id: str, new_order: NewOrder) -> PlacedOrder:
        """Replace an existing order."""
        return self.account.replace_order(self.session, order_id, new_order)

    def get_equity(self, symbol: str) -> Equity:
        """Get equity instrument."""
        return Equity.get_equity(self.session, symbol)

    def get_option(self, symbol: str) -> Option:
        """Get option instrument."""
        return Option.get_option(self.session, symbol)

    def get_option_chain(self, symbol: str) -> list[NestedOptionChain]:
        """Get option chain."""
        return NestedOptionChain.get_chain(self.session, symbol)

    # Singleton pattern
    _instance: Self | None = None

    @classmethod
    def get_instance(cls) -> Self:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def place_trade(
        self,
        instrument: Option | Equity,
        quantity: int,
        action: Literal["Buy to Open", "Sell to Close"],
        dry_run: bool = False
    ) -> str:
        """Place a trade for the given instrument"""
        try:
            # Get current price
            try:
                result = await self.get_prices(instrument.symbol)
                if isinstance(result, str):
                    return result
                bid, ask = result
                price = float(ask if action == "Buy to Open" else bid)
            except Exception as e:
                error_msg = f"Failed to get price for {instrument.symbol}: {str(e)}"
                logger.error(error_msg)
                return error_msg

            if action == "Buy to Open":
                multiplier = instrument.multiplier if hasattr(instrument, 'multiplier') else 1
                balances = await self.get_balances()
                order_value = Decimal(str(price)) * Decimal(str(quantity)) * Decimal(str(multiplier))

                # Use the appropriate buying power based on instrument type
                buying_power = (
                    balances.derivative_buying_power
                    if isinstance(instrument, Option)
                    else balances.equity_buying_power
                )

                if order_value > buying_power:
                    original_quantity = quantity
                    quantity = int(buying_power / (Decimal(str(price)) * Decimal(str(multiplier))))
                    logger.warning(
                        f"Reduced order quantity from {original_quantity} to {quantity} due to buying power limits"
                    )
                    if quantity <= 0:
                        error_msg = "Order rejected: Exceeds available funds"
                        logger.error(error_msg)
                        return error_msg

            else:  # Sell to Close
                positions = await self.get_positions()
                position = next((p for p in positions if p.symbol == instrument.symbol), None)
                if not position:
                    error_msg = f"No open position found for {instrument.symbol}"
                    logger.error(error_msg)
                    return f"Error: No open position found for {instrument.symbol}"

                orders = self.get_live_orders()
                pending_sell_quantity = sum(
                    sum(leg.quantity for leg in order.legs)
                    for order in orders
                    if (order.status in (OrderStatus.LIVE, OrderStatus.RECEIVED) and
                        any(leg.symbol == instrument.symbol and
                            leg.action == OrderAction.SELL_TO_CLOSE
                            for leg in order.legs))
                )

                available_quantity = position.quantity - pending_sell_quantity
                logger.info(
                    f"Position: {position.quantity}, Pending sells: {pending_sell_quantity}, Available: {available_quantity}"
                )

                if available_quantity <= 0:
                    error_msg = (
                        f"Cannot place order - entire position of {position.quantity} "
                        f"already has pending sell orders"
                    )
                    logger.error(error_msg)
                    return f"Error: {error_msg}"

                if quantity > available_quantity:
                    logger.warning(
                        f"Reducing sell quantity from {quantity} to {available_quantity} (maximum available)"
                    )
                    quantity = available_quantity

                if quantity <= 0:
                    error_msg = f"Position quantity ({available_quantity}) insufficient for requested sale"
                    logger.error(error_msg)
                    return f"Error: {error_msg}"

            order_action = OrderAction.BUY_TO_OPEN if action == "Buy to Open" else OrderAction.SELL_TO_CLOSE
            leg: Leg = instrument.build_leg(quantity, order_action)

            logger.info(
                f"Placing initial order: {action} {quantity} {instrument.symbol} @ ${price:.2f}"
            )

            initial_order = NewOrder(
                time_in_force=OrderTimeInForce.DAY,
                order_type=OrderType.LIMIT,
                legs=[leg],
                price=Decimal(str(price)) * (-1 if action == "Buy to Open" else 1)
            )

            response = self.place_order(initial_order, dry_run=dry_run)
            if response.errors:
                error_msg = "Order failed with errors:\n" + "\n".join(str(error) for error in response.errors)
                logger.error(error_msg)
                return error_msg

            if dry_run:
                msg = "Dry run successful"
                if response.warnings:
                    msg += "\nWarnings:\n" + "\n".join(str(w) for w in response.warnings)
                logger.info(msg)
                return msg

            current_order = response.order
            for attempt in range(20):
                await asyncio.sleep(15.0)

                orders = self.get_live_orders()
                order = next((o for o in orders if o.id == current_order.id), None)

                if not order:
                    error_msg = "Order not found during monitoring"
                    logger.error(error_msg)
                    return error_msg

                if order.status == OrderStatus.FILLED:
                    success_msg = "Order filled successfully"
                    logger.info(success_msg)
                    self.invalidate_positions()
                    self.invalidate_balances()
                    return success_msg

                if order.status not in (OrderStatus.LIVE, OrderStatus.RECEIVED):
                    error_msg = f"Order in unexpected status: {order.status}"
                    logger.error(error_msg)
                    return error_msg

                price_delta = 0.01 if action == "Buy to Open" else -0.01
                new_price = float(order.price) + price_delta
                logger.info(
                    f"Adjusting order price from ${float(order.price):.2f} to ${new_price:.2f} (attempt {attempt + 1}/20)"
                )

                new_order = NewOrder(
                    time_in_force=OrderTimeInForce.DAY,
                    order_type=OrderType.LIMIT,
                    legs=[leg],
                    price=Decimal(str(new_price)) * (-1 if action == "Buy to Open" else 1)
                )

                response = self.replace_order(order.id, new_order)
                if response.errors:
                    error_msg = f"Failed to adjust order: {response.errors}"
                    logger.error(error_msg)
                    return error_msg

                current_order = response.order

            final_msg = "Order not filled after 20 price adjustments"
            logger.warning(final_msg)
            return final_msg

        except Exception as e:
            logger.exception("Error placing trade")
            return f"Error placing trade: {str(e)}"