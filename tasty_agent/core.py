from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from mcp.server.fastmcp import Context
from pydantic import BaseModel
from tabulate import tabulate
from tastytrade import Account, Session

logger = logging.getLogger(__name__)

COMPACT_EMPTY_VALUES = (None, "", [], {})


def is_compact_empty(
    value: Any,
    *,
    drop_zero_string: bool = False,
    drop_numeric_zero: bool = False,
) -> bool:
    """Return whether a compacted value should be omitted from tool output."""
    if value in COMPACT_EMPTY_VALUES:
        return True
    if drop_zero_string and value == "0":
        return True
    return drop_numeric_zero and type(value) is not bool and value == 0


def compact_row(
    data: dict[str, Any],
    *,
    drop_zero_string: bool = False,
    drop_numeric_zero: bool = False,
) -> dict[str, Any]:
    """Drop empty values from an already compacted row."""
    return {
        key: value
        for key, value in data.items()
        if not is_compact_empty(
            value,
            drop_zero_string=drop_zero_string,
            drop_numeric_zero=drop_numeric_zero,
        )
    }


def compact_value(value: Any) -> Any:
    """Return a compact, JSON/table friendly scalar."""
    if isinstance(value, Decimal):
        if value.is_nan():
            return None
        text = format(value.normalize(), "f")
        return text.rstrip("0").rstrip(".") if "." in text else text
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, BaseModel):
        return compact_model_dump(value)
    if isinstance(value, list):
        return [compact_value(item) for item in value if item is not None]
    if isinstance(value, tuple):
        return tuple(compact_value(item) for item in value if item is not None)
    if isinstance(value, dict):
        return compact_dict(value)
    return value


def compact_dict(data: dict[str, Any], fields: Sequence[str] | None = None) -> dict[str, Any]:
    """Keep selected non-empty fields and compact scalar values."""
    keys = fields or data.keys()
    compacted: dict[str, Any] = {}
    for key in keys:
        if key not in data:
            continue
        value = compact_value(data[key])
        if is_compact_empty(value):
            continue
        compacted[key] = value
    return compacted


def compact_model_dump(model: BaseModel, fields: Sequence[str] | None = None) -> dict[str, Any]:
    """Dump a Pydantic model without empty fields or verbose Decimal/Enum objects."""
    return compact_dict(model.model_dump(), fields)


def to_table(data: Sequence[BaseModel] | Sequence[dict[str, Any]], fields: Sequence[str] | None = None) -> str:
    """Format rows as a compact plain table."""
    if not data:
        return "No data"
    rows = [
        compact_model_dump(item, fields) if isinstance(item, BaseModel) else compact_dict(item, fields) for item in data
    ]
    return tabulate(rows, headers="keys", tablefmt="plain", missingval="")


@dataclass
class ServerContext:
    session: Session
    account: Account


def get_context(ctx: Context) -> ServerContext:
    """Extract ServerContext from the MCP request context."""
    return ctx.request_context.lifespan_context


def get_session(ctx: Context) -> Session:
    """Get the tastytrade session (auto-refreshes tokens before each API call)."""
    return get_context(ctx).session


@asynccontextmanager
async def lifespan(_) -> AsyncIterator[ServerContext]:
    """Manage Tastytrade session lifecycle."""
    client_secret = os.getenv("TASTYTRADE_CLIENT_SECRET")
    refresh_token = os.getenv("TASTYTRADE_REFRESH_TOKEN")
    account_id = os.getenv("TASTYTRADE_ACCOUNT_ID")

    if not client_secret or not refresh_token:
        raise ValueError(
            "Missing Tastytrade OAuth credentials. Set TASTYTRADE_CLIENT_SECRET and "
            "TASTYTRADE_REFRESH_TOKEN environment variables."
        )

    try:
        session = Session(client_secret, refresh_token)
        accounts = await Account.get(session)
        logger.info("Successfully authenticated with Tastytrade. Found %s account(s).", len(accounts))
    except Exception as e:
        logger.error("Failed to authenticate with Tastytrade: %s", e)
        raise

    if account_id:
        account = next((acc for acc in accounts if acc.account_number == account_id), None)
        if not account:
            available = [acc.account_number for acc in accounts]
            raise ValueError(f"Account '{account_id}' not found. Available: {available}")
        logger.info("Using specified account: %s", account.account_number)
    else:
        account = accounts[0]
        logger.info("Using default account: %s", account.account_number)

    yield ServerContext(session=session, account=account)
