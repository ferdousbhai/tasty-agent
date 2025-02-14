from tastytrade.instruments import Option, Equity
from tastytrade.order import OrderAction, OrderStatus


class PositionService:
    """Service for managing positions"""
    def __init__(self, session, account):
        self.session = session
        self.account = account

    async def validate_sell_quantity(
        self,
        instrument: Option | Equity,
        quantity: int
    ) -> tuple[bool, int]:
        """Validate sell order quantity against current position."""
        positions = await self.account.a_get_positions(self.session)
        position = next((p for p in positions if p.symbol == instrument.symbol), None)
        
        if not position:
            return False, 0

        orders = self.account.get_live_orders(self.session)
        pending_sell_quantity = sum(
            sum(leg.quantity for leg in order.legs)
            for order in orders
            if (order.status in (OrderStatus.LIVE, OrderStatus.RECEIVED) and
                any(leg.symbol == instrument.symbol and
                    leg.action == OrderAction.SELL_TO_CLOSE
                    for leg in order.legs))
        )

        available_quantity = position.quantity - pending_sell_quantity
        return True, min(quantity, available_quantity)
