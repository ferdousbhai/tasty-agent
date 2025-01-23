from pydantic import BaseModel, Field
from decimal import Decimal
from datetime import date, datetime

class Position(BaseModel):
    symbol: str
    instrument_type: str
    underlying_symbol: str
    quantity: Decimal
    quantity_direction: str
    value: Decimal

class Balances(BaseModel):
    cash_balance: Decimal
    buying_power: Decimal
    net_liquidating_value: Decimal
    maintenance_excess: Decimal

class OptionExpirationIV(BaseModel):
    expiration_date: date
    implied_volatility: Decimal

class EarningsData(BaseModel):
    expected_report_date: date | None = None
    actual_eps: Decimal | None = None
    consensus_estimate: Decimal | None = None
    time_of_day: str | None = None

class RelevantMarketMetric(BaseModel):
    symbol: str
    implied_volatility_index: Decimal | None = None
    implied_volatility_index_rank: Decimal | None = None
    implied_volatility_percentile: Decimal | None = None
    liquidity_rating: int | None = None
    updated_at: datetime | None = None
    option_expiration_implied_volatilities: list[OptionExpirationIV] = Field(default_factory=list)
    beta: Decimal | None = None
    corr_spy_3month: Decimal | None = None
    market_cap: Decimal | None = None
    implied_volatility_30_day: Decimal | None = None
    historical_volatility_30_day: Decimal | None = None
    historical_volatility_60_day: Decimal | None = None
    historical_volatility_90_day: Decimal | None = None
    iv_hv_30_day_difference: Decimal | None = None
    earnings: EarningsData | None = None
