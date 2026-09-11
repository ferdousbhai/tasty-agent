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
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"Cannot compact non-finite Decimal value: {value}")
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
        return [compact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(compact_value(item) for item in value)
    if isinstance(value, dict):
        return compact_dict(value)
    return value


def compact_dict(data: dict[str, Any]) -> dict[str, Any]:
    compacted: dict[str, Any] = {}
    for key, raw_value in data.items():
        value = compact_value(raw_value)
        if is_compact_empty(value):
            continue
        compacted[key] = value
    return compacted


def compact_model_dump(model: BaseModel) -> dict[str, Any]:
    return compact_dict(model.model_dump())


def to_table(data: Sequence[BaseModel] | Sequence[dict[str, Any]]) -> str:
    if not data:
        return "No data"
    rows = [compact_model_dump(item) if isinstance(item, BaseModel) else compact_dict(item) for item in data]
    return tabulate(rows, headers="keys", tablefmt="plain", missingval="")


@dataclass
class ServerContext:
    session: Session
    account: Account


def select_account(accounts: list[Account], account_id: str | None) -> Account:
    if not accounts:
        raise ValueError("No Tastytrade accounts are available for these credentials.")
    if account_id:
        account = next(
            (candidate for candidate in accounts if candidate.account_number == account_id),
            None,
        )
        if account is None:
            available = [candidate.account_number for candidate in accounts]
            raise ValueError(f"Account '{account_id}' not found. Available: {available}")
        return account
    if len(accounts) > 1:
        available = [candidate.account_number for candidate in accounts]
        raise ValueError(
            f"TASTYTRADE_ACCOUNT_ID is required when credentials expose multiple accounts. Available: {available}"
        )
    return accounts[0]


def get_context(ctx: Context) -> ServerContext:
    return ctx.request_context.lifespan_context


def get_session(ctx: Context) -> Session:
    """The SDK refreshes authentication tokens before each API call."""
    return get_context(ctx).session


@asynccontextmanager
async def lifespan(_) -> AsyncIterator[ServerContext]:
    client_secret = os.getenv("TASTYTRADE_CLIENT_SECRET")
    refresh_token = os.getenv("TASTYTRADE_REFRESH_TOKEN")
    account_id = os.getenv("TASTYTRADE_ACCOUNT_ID")

    if not client_secret or not refresh_token:
        raise ValueError(
            "Missing Tastytrade OAuth credentials. Set TASTYTRADE_CLIENT_SECRET and "
            "TASTYTRADE_REFRESH_TOKEN environment variables."
        )
    if account_id is not None and not account_id.strip():
        raise ValueError("TASTYTRADE_ACCOUNT_ID must not be blank when configured.")

    # Session owns an httpx AsyncClient; its context manager is what releases the
    # connection pool when the server shuts down (or if authentication fails).
    async with Session(client_secret, refresh_token) as session:
        try:
            accounts = await Account.get(session)
            logger.info("Successfully authenticated with Tastytrade. Found %s account(s).", len(accounts))
        except Exception as e:
            logger.error("Failed to authenticate with Tastytrade: %s", e, exc_info=True)
            raise

        account = select_account(accounts, account_id)
        if account_id:
            logger.info("Using specified account: %s", account.account_number)
        else:
            logger.info("Using sole account: %s", account.account_number)

        yield ServerContext(session=session, account=account)
