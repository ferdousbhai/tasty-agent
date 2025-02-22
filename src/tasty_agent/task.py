import logging
import asyncio
from typing import Literal

from pydantic import BaseModel
from tastytrade.instruments import Option, Equity

from .tastytrade_api import TastytradeAPI
from ..utils import is_market_open, get_time_until_market_open, format_time_delta

logger = logging.getLogger(__name__)

class Task(BaseModel):
    """Represents a scheduled task"""
    task_id: str
    quantity: int
    action: Literal["Buy to Open", "Sell to Close"]
    instrument: Option | Equity
    dry_run: bool = False
    description: str | None = None
    _task: asyncio.Task | None = None

    def get_execution_time_info(self) -> str:
        """Get information about when the task will execute"""
        if is_market_open():
            return "the trade will execute immediately"
        time_until = get_time_until_market_open()
        return f"the trade will execute when market opens: in {format_time_delta(time_until)}"

    async def execute(self):
        """Execute the task"""
        try:
            if not is_market_open():
                logger.warning(
                    f"Market closed, waiting for next market open for task {self.task_id}"
                )
                await asyncio.sleep(get_time_until_market_open().total_seconds())

            api = TastytradeAPI.get_instance()
            result = await api.place_trade(
                instrument=self.instrument,
                quantity=self.quantity,
                action=self.action,
                dry_run=self.dry_run
            )

            logger.info(f"Task {self.task_id} executed successfully: {result}")
            return result

        except Exception as e:
            error_msg = f"Task {self.task_id} failed: {str(e)}"
            logger.error(error_msg)
            return error_msg
