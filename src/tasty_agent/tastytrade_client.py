import asyncio
from datetime import datetime, timedelta, date
from decimal import Decimal
from typing import Literal, Self
import keyring
import logging
import os

from tastytrade import Session, Account, metrics
from tastytrade.account import AccountBalance, CurrentPosition
from tastytrade.order import NewOrder, PlacedOrder, OrderStatus, OrderAction, OrderTimeInForce, OrderType, Leg
from tastytrade.instruments import Option, Equity, NestedOptionChain
from tastytrade.streamer import DXLinkStreamer
from tastytrade.dxfeed import Quote

logger = logging.getLogger(__name__)

class TastytradeClient:
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
        self.username = keyring.get_password("tastytrade", "username") or os.getenv("TASTYTRADE_USERNAME")
        self.password = keyring.get_password("tastytrade", "password") or os.getenv("TASTYTRADE_PASSWORD")
        self.account_id = keyring.get_password("tastytrade", "account_id") or os.getenv("TASTYTRADE_ACCOUNT_ID")

        if not self.username or not self.password:
            raise ValueError(
                "Missing Tastytrade credentials. Please run 'tasty-agent setup' or set "
                "TASTYTRADE_USERNAME and TASTYTRADE_PASSWORD environment variables."
            )

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
        # If no option parameters, treat as equity
        if not any([expiration_date, option_type, strike]):
            return Equity.get_equity(self.session, underlying_symbol)

        # Validate all option parameters are present
        if not all([expiration_date, option_type, strike]):
            logger.error("Must provide all option parameters (expiration_date, option_type, strike) or none")
            return None

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
                except ValueError as e:
                    return f"Invalid expiration date format: {e}. Use YYYY-MM-DD format."

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
            logger.error(f"Error getting prices for {underlying_symbol}: {str(e)}")
            return f"Error getting prices for {underlying_symbol}: {str(e)}"

    async def get_quote(self, streamer_symbol: str) -> tuple[Decimal, Decimal] | str:
        """Get current quote for a symbol."""
        try:
            async with DXLinkStreamer(self.session) as streamer:
                await streamer.subscribe(Quote, [streamer_symbol])
                # Get the quote
                quote = await asyncio.wait_for(streamer.get_event(Quote), timeout=10.0)
                return Decimal(str(quote.bid_price)), Decimal(str(quote.ask_price))
        except asyncio.TimeoutError:
            return f"Timed out waiting for quote data for {streamer_symbol}"
        except asyncio.CancelledError:
            # Handle WebSocket cancellation explicitly
            logger.warning(f"WebSocket connection interrupted for {streamer_symbol}")
            return f"WebSocket connection interrupted for {streamer_symbol}"
        except Exception as e:
            # Catch all other exceptions
            logger.error(f"Error getting quote for {streamer_symbol}: {str(e)}")
            return f"Error getting quote for {streamer_symbol}: {str(e)}"

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

    # Barebone delete_order, mirrors replace_order structure
    def delete_order(self, order_id: str) -> dict:
        """delete an existing live order."""
        return self.account.delete_order(self.session, order_id)

    # Singleton pattern
    _instance: Self | None = None

    @classmethod
    def get_instance(cls) -> Self:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def place_trade(
        self,
        underlying_symbol: str,
        quantity: int,
        action: Literal["Buy to Open", "Sell to Close"],
        expiration_date: str | None = None,
        option_type: Literal["C", "P"] | None = None,
        strike: float | None = None,
        dry_run: bool = False,
        job_id: str | None = None,
    ) -> tuple[bool, str]:
        """Place a trade with the specified parameters.

        Args:
            underlying_symbol: The symbol of the stock or underlying for an option
            quantity: Number of shares/contracts
            action: Buy to Open or Sell to Close
            expiration_date: Option expiration date in YYYY-MM-DD format (None for equity)
            option_type: Option type, 'C' for call or 'P' for put (None for equity)
            strike: Option strike price (None for equity)
            dry_run: If True, simulate without executing
            job_id: Optional ID for logging purposes

        Returns:
            Tuple of (success, message)
        """
        log_prefix = f"[Job: {job_id}] " if job_id else ""
        original_requested_quantity = quantity # Store for logging

        try:
            # --- Instrument Creation and Validation ---
            expiry_datetime = None
            if expiration_date:
                try:
                    expiry_datetime = datetime.strptime(expiration_date, "%Y-%m-%d")
                except ValueError as e:
                    raise ValueError(f"Invalid expiration date format: {e}. Use YYYY-MM-DD format.")

            instrument = await self.create_instrument(
                underlying_symbol=underlying_symbol,
                expiration_date=expiry_datetime,
                option_type=option_type,
                strike=strike
            )
            if instrument is None:
                error_msg = f"Could not create instrument for symbol: {underlying_symbol}"
                if expiration_date:
                    error_msg += f" with expiration {expiration_date}, type {option_type}, strike {strike}"
                raise ValueError(error_msg)

            # --- Price Fetching ---
            try:
                bid, ask = await self.get_quote(instrument.streamer_symbol)
                price = float(ask if action == "Buy to Open" else bid)
            except Exception as e:
                raise ValueError(f"Failed to get price for {instrument.symbol}: {str(e)}")

            # --- Pre-Trade Checks ---
            # This inner try-except handles errors specific to pre-trade validation
            try:
                if action == "Buy to Open":
                    multiplier = instrument.multiplier if hasattr(instrument, 'multiplier') else 1
                    balances = await self.get_balances()
                    order_value = Decimal(str(price)) * Decimal(str(quantity)) * Decimal(str(multiplier))

                    buying_power = (
                        balances.derivative_buying_power
                        if isinstance(instrument, Option)
                        else balances.equity_buying_power
                    )

                    if order_value > buying_power:
                        adjusted_quantity = int(buying_power / (Decimal(str(price)) * Decimal(str(multiplier))))
                        if adjusted_quantity <= 0:
                            raise ValueError(f"Order rejected: Insufficient buying power (${buying_power:,.2f}) for even 1 unit @ ${price:.2f} (Value: ${Decimal(str(price)) * Decimal(str(multiplier)):,.2f})")
                        logger.warning(
                            f"{log_prefix}Reduced order quantity from {original_requested_quantity} to {adjusted_quantity} "
                            f"due to buying power limit (${buying_power:,.2f} < ${order_value:,.2f})"
                        )
                        quantity = adjusted_quantity # Update quantity for the order

                else:  # Sell to Close
                    positions = await self.get_positions()
                    position = next((p for p in positions if p.symbol == instrument.symbol), None)
                    if not position:
                        raise ValueError(f"No open position found for {instrument.symbol}")

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
                        f"{log_prefix}Position: {position.quantity}, Pending sells: {pending_sell_quantity}, Available: {available_quantity}"
                    )

                    if available_quantity <= 0:
                        raise ValueError(
                            f"Cannot place order - entire position of {position.quantity} "
                            f"already has pending sell orders ({pending_sell_quantity})"
                        )

                    if quantity > available_quantity:
                        logger.warning(
                            f"{log_prefix}Reducing sell quantity from {original_requested_quantity} to {available_quantity} (maximum available)"
                        )
                        quantity = available_quantity

                    if quantity <= 0: # Should be unreachable due to checks above, but safeguard
                         raise ValueError(f"Calculated available quantity ({available_quantity}) is zero or less.")

            except ValueError as pre_trade_error:
                 # Re-raise pre-trade check errors to be caught by the outer handler
                 raise pre_trade_error

        except ValueError as setup_or_check_error:
            # Catch errors from instrument creation, pricing, or pre-trade checks
            logger.error(f"{log_prefix}{str(setup_or_check_error)}")
            return False, f"Error: {str(setup_or_check_error)}"
        except Exception as e:
            # Catch unexpected errors during setup/checks
            error_msg = f"Unexpected error during trade setup/checks for {underlying_symbol}: {str(e)}"
            logger.exception(f"{log_prefix}{error_msg}") # Log with stack trace
            return False, error_msg


        # --- Order Placement with Retry Logic ---
        max_placement_retries = 10 # Number of times to retry by reducing quantity (only for buy)
        placed_order_response = None
        final_quantity = quantity # Track the quantity that gets successfully placed

        for attempt in range(max_placement_retries + 1):
            current_attempt_quantity = quantity # Use a temporary variable for quantity in this attempt

            if current_attempt_quantity <= 0:
                # This check is primarily for the buy-side retry logic below
                error_msg = f"Cannot place order, quantity reduced to zero during placement attempts (Attempt {attempt+1})."
                logger.error(f"{log_prefix}{error_msg}")
                return False, "Order rejected: Exceeds available funds after adjustments for fees/margin."

            # --- Build Leg and Order Details for Current Attempt ---
            order_action = OrderAction.BUY_TO_OPEN if action == "Buy to Open" else OrderAction.SELL_TO_CLOSE
            leg: Leg = instrument.build_leg(current_attempt_quantity, order_action)

            logger.info(
                f"{log_prefix}Attempting order placement (Attempt {attempt+1}/{max_placement_retries+1}): "
                f"{action} {current_attempt_quantity} {instrument.symbol} @ ${price:.2f}"
            )

            current_order_details = NewOrder(
                time_in_force=OrderTimeInForce.DAY,
                order_type=OrderType.LIMIT,
                legs=[leg],
                # Use the initially fetched price for placement attempts
                price=Decimal(str(price)) * (-1 if action == "Buy to Open" else 1)
            )

            # --- Attempt to Place Order ---
            try:
                response = self.place_order(current_order_details, dry_run=dry_run)

                if not response.errors:
                    # --- Successful Placement ---
                    placed_order_response = response
                    final_quantity = current_attempt_quantity # Store the quantity that worked
                    logger.info(f"{log_prefix}Order placement successful for quantity {final_quantity} (ID: {response.order.id if not dry_run and response.order else 'N/A - Dry Run'})")
                    break # Exit the placement retry loop

                # --- Handle Placement Errors ---
                else:
                    # Check for insufficient funds/buying power/margin errors
                    is_insufficient_funds_error = any(
                        "buying power" in str(e).lower() or
                        "insufficient funds" in str(e).lower() or
                        "margin requirement" in str(e).lower()
                        for e in response.errors
                    )

                    # Retry condition: Buy order, funds error, quantity > 0, retries left
                    if (action == "Buy to Open" and
                        is_insufficient_funds_error and
                        quantity > 1 and # Check if quantity can be reduced
                        attempt < max_placement_retries):

                        quantity -= 1 # Reduce persistent quantity for the *next* attempt
                        logger.warning(
                            f"{log_prefix}Placement failed likely due to funds/fees. Errors: {response.errors}. "
                            f"Reducing quantity to {quantity} and retrying."
                        )
                        await asyncio.sleep(0.5) # Small delay before next attempt
                        continue # Go to next iteration of the placement loop
                    else:
                        # Non-recoverable error, or Sell order error, or max retries hit, or quantity became 0/1
                        error_msg = (f"Order placement failed permanently (Attempt {attempt+1}):\n"
                                     + "\n".join(str(error) for error in response.errors))
                        logger.error(f"{log_prefix}{error_msg}")
                        return False, error_msg # Exit function with failure

            except Exception as e:
                 # Catch unexpected exceptions during the place_order call itself
                 error_msg = f"Exception during order placement attempt {attempt+1}: {str(e)}"
                 logger.exception(f"{log_prefix}{error_msg}") # Log with stack trace
                 return False, error_msg # Exit function on unexpected exception

        # --- After Placement Loop ---
        if not placed_order_response:
             # Should only be reachable if the loop finished without breaking (e.g., retries exhausted)
             error_msg = f"Order placement failed after {max_placement_retries + 1} attempts, likely due to persistent insufficient funds/fees."
             logger.error(f"{log_prefix}{error_msg}")
             return False, error_msg

        # --- Handle Dry Run Success ---
        if dry_run:
            msg = f"Dry run successful (Simulated: {action} {final_quantity} {instrument.symbol} @ ${price:.2f})"
            if placed_order_response.warnings:
                msg += "\nWarnings:\n" + "\n".join(str(w) for w in placed_order_response.warnings)
            logger.info(f"{log_prefix}{msg}")
            return True, msg

        # --- Live Order Monitoring (Post Successful Placement) ---
        current_order = placed_order_response.order
        if not current_order:
             # Should not happen if not dry_run and placement succeeded, but safeguard
             error_msg = "Order object not found in successful placement response."
             logger.error(f"{log_prefix}{error_msg}")
             return False, error_msg

        logger.info(f"{log_prefix}Monitoring placed order {current_order.id} (Qty: {final_quantity}) for fill...")

        # Prepare the leg with the *final* quantity for potential replacements
        final_order_action = OrderAction.BUY_TO_OPEN if action == "Buy to Open" else OrderAction.SELL_TO_CLOSE
        final_leg: Leg = instrument.build_leg(final_quantity, final_order_action)


        # --- Price Adjustment / Fill Monitoring Loop ---
        for fill_attempt in range(20): # Existing monitoring loop
            await asyncio.sleep(15.0)

            orders = self.get_live_orders()
            order = next((o for o in orders if o.id == current_order.id), None)

            if not order:
                # If order disappears, it might have been filled very quickly or deleteled externally
                error_msg = f"Order {current_order.id} not found during monitoring. It might have filled or been cancelled."
                logger.warning(f"{log_prefix}{error_msg}")
                # Invalidate cache as state is unclear and return uncertainty
                self.invalidate_positions()
                self.invalidate_balances()
                return False, error_msg

            if order.status == OrderStatus.FILLED:
                success_msg = f"Order {order.id} filled successfully (Qty: {final_quantity})"
                logger.info(f"{log_prefix}{success_msg}")
                self.invalidate_positions()
                self.invalidate_balances()
                return True, success_msg

            if order.status not in (OrderStatus.LIVE, OrderStatus.RECEIVED):
                error_msg = f"Order {order.id} entered unexpected status during monitoring: {order.status}"
                logger.error(f"{log_prefix}{error_msg}")
                return False, error_msg # Terminal failure state

            # --- Adjust Price if Still Live ---
            price_delta = 0.01 if action == "Buy to Open" else -0.01
            try:
                 current_price_float = float(order.price)
            except (ValueError, TypeError):
                 error_msg = f"Could not parse current order price '{order.price}' as float for adjustment."
                 logger.error(f"{log_prefix}{error_msg}")
                 return False, error_msg

            new_price = current_price_float + price_delta
            logger.info(
                f"{log_prefix}Adjusting order price from ${current_price_float:.2f} to ${new_price:.2f} "
                f"(Fill Attempt {fill_attempt + 1}/20)"
            )

            # Use the final_leg with the correct placed quantity
            replacement_order_details = NewOrder(
                time_in_force=OrderTimeInForce.DAY,
                order_type=OrderType.LIMIT,
                legs=[final_leg], # Use the leg matching the successfully placed quantity
                price=Decimal(str(new_price)) * (-1 if action == "Buy to Open" else 1)
            )

            try:
                replace_response = self.replace_order(order.id, replacement_order_details)
                if replace_response.errors:
                    # Log error but continue monitoring loop - adjustment failure shouldn't stop fill check
                    error_msg = f"Failed to adjust order {order.id}: {replace_response.errors}"
                    logger.error(f"{log_prefix}{error_msg}")
                    # Don't return false here, let the loop continue
                else:
                     current_order = replace_response.order # Update reference to potentially new order state

            except Exception as e:
                 # Log exception but continue monitoring loop
                 error_msg = f"Exception during order replacement for {order.id}: {str(e)}"
                 logger.exception(f"{log_prefix}{error_msg}")
                 # Don't return false here

        # --- Monitoring Loop Completed Without Fill ---
        final_msg = f"Order {current_order.id} not filled after 20 price adjustments."
        logger.warning(f"{log_prefix}{final_msg}")

        # Attempt to delete the lingering order - fire and forget
        try:
            self.delete_order(current_order.id)
        except Exception as e:
            # Log the error if deletion fails, but don't change the return message
            logger.error(f"{log_prefix}Failed to delete lingering order {current_order.id}: {str(e)}")
        # Return False because the trade did not fill
        return False, final_msg