import asyncio
import json
import logging
from typing import Literal
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from src.tastytrade_api.functions import get_bid_ask_price, buy_to_open, sell_to_close

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parents[2] # this file is in src/core
DATA_DIR = PROJECT_ROOT / "data"

class OrderManager:
    """
    Encapsulates the order queue and related logic.
    """

    def __init__(self, queue_file: Path = DATA_DIR / "order_queue.json"):
        # Create data directory if it doesn't exist
        DATA_DIR.mkdir(exist_ok=True)

        self.queue_file = queue_file
        # Holds all queued tasks by group: {group_number: [ {order_item_dict}, ... ], ...}
        self.task_queue: dict[int, list[dict]] = {}

    def load_queue_from_file(self) -> None:
        """
        Loads tasks from self.queue_file into self.task_queue (in-memory).
        If the file is missing or empty, this is a no-op.
        """
        if not self.queue_file.is_file():
            return

        try:
            logger.info(f"Looking for: {self.queue_file.resolve()}")
            with open(self.queue_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            # Convert keys back from string into int
            self.task_queue = {int(k): v for k, v in data.items()}
            logger.info("Loaded %d groups from %s", len(self.task_queue), self.queue_file)
        except Exception as e:
            logger.error("Error loading %s: %s", self.queue_file, str(e))

    def save_queue_to_file(self) -> None:
        """
        Saves the current self.task_queue to disk in JSON format.
        """
        serializable = {str(k): v for k, v in self.task_queue.items()}
        try:
            with open(self.queue_file, "w", encoding="utf-8") as f:
                json.dump(serializable, f, indent=2)
            logger.info("Successfully saved queue to %s", self.queue_file)
        except Exception as e:
            logger.error("Error saving %s: %s", self.queue_file, str(e))

    async def queue_order(
        self,
        symbol: str,
        quantity: int,
        action: Literal['BUY_TO_OPEN', 'SELL_TO_CLOSE'],
        execution_group: int = 1,
        dry_run: bool = True
    ) -> str:
        """
        Queues an order for execution, storing symbol, quantity, action, group, and dry_run.
        The actual bid/ask retrieval and limit price calculation now occur at runtime
        within execute_queued_tasks().
        """
        # Load current datastore
        self.load_queue_from_file()

        if execution_group not in self.task_queue:
            self.task_queue[execution_group] = []

        # We no longer pre-calculate price here:
        self.task_queue[execution_group].append({
            "symbol": symbol,
            "quantity": quantity,
            "action": action,
            "dry_run": dry_run
        })

        # Save updated queue to disk
        self.save_queue_to_file()

        return (
            f"Order queued: symbol={symbol}, qty={quantity}, "
            f"action={action}, (execution_group={execution_group})."
        )

    async def execute_queued_tasks(self, session, account):
        """
        Executes all queued tasks (in ascending order of their execution_group).
        Within the same group, tasks run in parallel (async).
        """
        self.load_queue_from_file()
        sorted_groups = sorted(self.task_queue.keys())
        successful_groups = set()

        for group in sorted_groups:
            tasks = self.task_queue[group]
            if not tasks:
                continue

            logger.info("Executing group=%s with %d tasks...", group, len(tasks))
            coros = []
            for idx, t in enumerate(tasks):
                bid, ask = await get_bid_ask_price(session, t["symbol"])
                logger.info("Fetched bid=%s, ask=%s for symbol=%s", bid, ask, t["symbol"])
                
                limit_price = Decimal((bid + ask) / 2).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                
                # Map action to corresponding function
                action_map = {
                    "Buy to Open": buy_to_open,
                    "Sell to Close": sell_to_close
                }
                
                if t["action"] not in action_map:
                    logger.error("Unsupported action %s for task %d in group %s", t["action"], idx, group)
                    continue
                    
                coros.append(action_map[t["action"]](
                    session,
                    account,
                    t["symbol"],
                    t["quantity"],
                    float(limit_price),
                    dry_run=t["dry_run"],
                ))

            results = await asyncio.gather(*coros, return_exceptions=True)
            group_success = True
            
            for idx, res in enumerate(results):
                if isinstance(res, Exception) or (isinstance(res, str) and any(x in res.lower() for x in ["rejected", "error"])):
                    logger.error("Task %d in group %s failed: %s", idx, group, str(res))
                    group_success = False
                else:
                    logger.info("Task %d in group %s completed: %s", idx, group, res)

            if group_success:
                successful_groups.add(group)

        # Remove successful groups and save
        for group in successful_groups:
            del self.task_queue[group]
        self.save_queue_to_file()
        
        if failed_groups := set(sorted_groups) - successful_groups:
            logger.warning("Some groups failed execution: %s", failed_groups)
        else:
            logger.info("All queued tasks have been executed successfully.")

    def cancel_queued_orders(self, execution_group: int | None = None, symbol: str | None = None) -> str:
        """
        Cancels queued orders based on provided filters.

        Args:
            execution_group: If provided, only cancels orders in this group.
                            If None, considers orders in all groups.
            symbol: If provided, only cancels orders for this symbol.
                    If None, considers all symbols.

        Returns:
            A message describing what was cancelled.
        """
        # Load current queue state
        self.load_queue_from_file()

        cancelled_count = 0
        if execution_group is not None:
            # Cancel orders in specific group
            if execution_group in self.task_queue:
                if symbol is not None:
                    # Remove only orders matching the symbol
                    original_len = len(self.task_queue[execution_group])
                    self.task_queue[execution_group] = [
                        order for order in self.task_queue[execution_group]
                        if order["symbol"] != symbol
                    ]
                    cancelled_count = original_len - len(self.task_queue[execution_group])
                else:
                    # Remove all orders in the group
                    cancelled_count = len(self.task_queue[execution_group])
                    del self.task_queue[execution_group]
        else:
            # Process all groups
            groups_to_delete = []
            for group, orders in self.task_queue.items():
                if symbol is not None:
                    # Remove only orders matching the symbol
                    original_len = len(orders)
                    self.task_queue[group] = [
                        order for order in orders
                        if order["symbol"] != symbol
                    ]
                    cancelled_count += original_len - len(self.task_queue[group])
                    if not self.task_queue[group]:
                        groups_to_delete.append(group)
                else:
                    # Remove all orders
                    cancelled_count += len(orders)
                    groups_to_delete.append(group)

            # Clean up empty groups
            for group in groups_to_delete:
                del self.task_queue[group]

        # Save updated queue
        self.save_queue_to_file()

        return f"Cancelled {cancelled_count} order{'s' if cancelled_count != 1 else ''}"