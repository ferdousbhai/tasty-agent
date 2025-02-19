import logging
from typing import Literal
import asyncio

from pydantic import BaseModel
from tastytrade.instruments import Option, Equity

from .trading import place_trade
from ..utils import is_market_open, get_time_until_market_open

# Add logger configuration
logger = logging.getLogger(__name__)

class Task(BaseModel):
    """Represents a scheduled task"""
    task_id: str
    quantity: int
    action: Literal["Buy to Open", "Sell to Close"]
    instrument: Option | Equity
    dry_run: bool = False
    description: str | None = None
    schedule_type: Literal["immediate", "once", "daily"] = "once"
    run_time: str | None = None
    _task: asyncio.Task | None = None

    async def execute(self):
        """Execute the task"""
        try:
            if not is_market_open():
                logger.warning(
                    f"Market closed, waiting for next market open for task {self.task_id}"
                )
                await asyncio.sleep(get_time_until_market_open().total_seconds())

            result = await place_trade(
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

# Task Storage
scheduled_tasks: dict[str, Task] = {}
