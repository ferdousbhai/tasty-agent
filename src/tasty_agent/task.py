from typing import Literal
import asyncio
from pydantic import BaseModel

from .common import mcp
from .trading import place_trade
from ..utils import is_market_open, get_time_until_market_open


class Task(BaseModel):
    """Represents a scheduled task"""
    task_id: str
    symbol: str
    quantity: int
    action: Literal["Buy to Open", "Sell to Close"]
    dry_run: bool = True
    description: str | None = None
    schedule_type: Literal["immediate", "once", "daily"] = "once"
    run_time: str | None = None
    _task: asyncio.Task | None = None  # to store the running task

    async def execute(self):
        """Execute the task"""
        try:
            if not is_market_open():
                mcp.send_log_message(level="warning", data=f"Market closed, waiting for next market open for task {self.task_id}")
                await asyncio.sleep(get_time_until_market_open().total_seconds())

            result = await place_trade(
                symbol=self.symbol,
                quantity=self.quantity,
                action=self.action,
                dry_run=self.dry_run
            )

            mcp.send_log_message(level="info", data=f"Task {self.task_id} executed successfully: {result}")
            return result

        except Exception as e:
            error_msg = f"Task {self.task_id} failed: {str(e)}"
            mcp.send_log_message(level="error", data=error_msg)
            return error_msg

# Task Storage
scheduled_tasks: dict[str, Task] = {}
