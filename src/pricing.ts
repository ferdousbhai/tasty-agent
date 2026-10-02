import { Decimal } from 'decimal.js'

import { decimalText } from './compact.js'
import { isBuyAction, type InstrumentDetail, type ResolvedLeg } from './instruments.js'
import { tickSizeAt } from './tastytrade/tick-sizes.js'

/** Warn when the resolved limit sits further from mid than the larger of these and one tick. */
const MID_DISTANCE_WARNING_FLOOR = new Decimal('0.05')
const MID_DISTANCE_WARNING_SPREAD_FRACTION = new Decimal('0.25')

/** A validated two-sided quote (see `legQuote`). */
export type LegQuote = { bid: Decimal; ask: Decimal }
/** One order leg with its resolved instrument and live quote. */
export type MarketLeg = ResolvedLeg & { quote: LegQuote }

/**
 * The signed net market for one order.
 *
 * Prices describe one reduced leg-ratio unit regardless of submitted quantity: 100 shares or 17
 * contracts keep the per-share/per-contract price, while a 17:17 vertical is priced as one 1:1
 * spread. `naturalPrice` is the marketable side (buys at ask, sells at bid); `passivePrice` the
 * optimistic side (buys at bid, sells at ask). Tastytrade's signed convention applies throughout:
 * debits are negative and credits positive.
 */
interface OrderMarket {
  naturalPrice: Decimal
  passivePrice: Decimal
  midPrice: Decimal
  spread: Decimal
  tickSize: Decimal
}

interface OrderSizing {
  targetValue: Decimal
  unitValue: Decimal
  quantity: number
  estimatedValue: Decimal
}

function money(value: Decimal): string {
  const amount = value.abs().toFixed(2, Decimal.ROUND_HALF_EVEN)
  return `${value.isNegative() && !value.isZero() ? '-' : ''}$${amount}`
}

export function formatOrderMarket(market: OrderMarket): string {
  return (
    `natural=${money(market.naturalPrice)}, mid=${money(market.midPrice)}, passive=${money(market.passivePrice)}, ` +
    `spread=${money(market.spread)}, tick=${money(market.tickSize)}`
  )
}

function roundToTick(value: Decimal, tickSize: Decimal): Decimal {
  return value.div(tickSize).toDecimalPlaces(0, Decimal.ROUND_HALF_UP).mul(tickSize)
}

function instrumentTickSize(detail: InstrumentDetail, price: Decimal): Decimal {
  const tick = detail.tick
  if (!tick) {
    throw new Error(
      `Missing broker tick sizes for ${detail.label}. Cannot safely round the order price to the broker's tick grid.`,
    )
  }
  return 'size' in tick ? tick.size : tickSizeAt(tick.tiers, price, detail.label)
}

function gcd(left: number, right: number): number {
  return right === 0 ? left : gcd(right, left % right)
}

/** The greatest common divisor of the leg quantities: 1 when they are already the smallest ratio. */
export function quantityGcd(quantities: readonly number[]): number {
  if (!quantities.length) throw new Error('At least one order leg is required')
  return quantities.reduce(gcd, 0)
}

export function buildOrderMarket(legs: readonly MarketLeg[]): OrderMarket {
  const unitSize = quantityGcd(legs.map((leg) => leg.quantity))
  let naturalPrice = new Decimal(0)
  let passivePrice = new Decimal(0)
  const legTicks = legs.map(({ detail, action, quantity, quote: { bid, ask } }) => {
    const ratio = new Decimal(quantity).div(unitSize)
    const sign = isBuyAction(action) ? -1 : 1
    naturalPrice = naturalPrice.plus((sign < 0 ? ask : bid).mul(ratio).mul(sign))
    passivePrice = passivePrice.plus((sign < 0 ? bid : ask).mul(ratio).mul(sign))
    return instrumentTickSize(detail, bid.plus(ask).div(2))
  })
  // A multi-leg order must sit on every leg's grid, so the coarsest leg tick governs.
  return {
    naturalPrice,
    passivePrice,
    midPrice: naturalPrice.plus(passivePrice).div(2),
    spread: passivePrice.minus(naturalPrice),
    tickSize: Decimal.max(...legTicks),
  }
}

/** The tick-aligned mid limit, kept strictly inside the market whenever a tick fits there. */
export function resolveOrderPrice(market: OrderMarket): { price: Decimal; warnings: string[] } {
  const { naturalPrice, passivePrice, tickSize } = market
  const tickFitsInside = market.spread.gt(tickSize)
  let price = roundToTick(market.midPrice, tickSize)
  if (tickFitsInside && price.lte(naturalPrice)) price = roundToTick(naturalPrice.plus(tickSize), tickSize)
  if (tickFitsInside && price.gte(passivePrice)) price = roundToTick(passivePrice.minus(tickSize), tickSize)

  const warnings: string[] = []
  const outside = price.lt(naturalPrice) || price.gt(passivePrice)
  const onBoundary = price.eq(naturalPrice) || price.eq(passivePrice)
  if ((outside || onBoundary) && tickFitsInside) {
    throw new Error(`Limit price ${money(price)} must be strictly inside the current order market (${formatOrderMarket(market)}).`)
  }
  if (outside || onBoundary) {
    warnings.push(
      'No valid tick price exists strictly inside the current bid/ask spread; ' +
        `using ${outside ? 'nearest valid tick' : 'boundary price'} ${money(price)} for ${formatOrderMarket(market)}.`,
    )
  }

  const threshold = Decimal.max(MID_DISTANCE_WARNING_FLOOR, market.spread.mul(MID_DISTANCE_WARNING_SPREAD_FRACTION), tickSize)
  const distance = price.minus(market.midPrice).abs()
  if (distance.gt(threshold)) {
    warnings.push(
      `Limit price ${money(price)} is ${money(distance)} from mid ${money(market.midPrice)}; ` +
        `warning threshold is ${money(threshold)}. Verify the user intended this aggressive price.`,
    )
  }
  return { price, warnings }
}

function dollarMultiplier(details: readonly InstrumentDetail[]): Decimal {
  const multipliers = details.map((detail) => {
    if (detail.kind === 'Equity') return new Decimal(1)
    if (!detail.sharesPerContract) throw new Error(`target_value sizing needs a share multiplier, which ${detail.label} does not have`)
    return detail.sharesPerContract
  })
  if (new Set(multipliers.map(String)).size !== 1) {
    throw new Error('target_value sizing requires all legs to share the same dollar multiplier')
  }
  return multipliers[0]!
}

/**
 * How many units of the (already reduced) leg ratio `targetValue` buys at `price`. The input schema
 * guarantees the ratio is reduced and the legs are equities or equity options.
 */
export function sizeOrder(details: readonly InstrumentDetail[], price: Decimal, targetValue: Decimal): OrderSizing {
  const unitValue = price.abs().mul(dollarMultiplier(details))
  const quantity = unitValue.isZero() ? 0 : targetValue.div(unitValue).floor().toNumber()
  if (quantity < 1) {
    throw new Error(`target_value $${decimalText(targetValue)} is too small for one order unit at ${money(unitValue)}.`)
  }
  return { targetValue, unitValue, quantity, estimatedValue: unitValue.mul(quantity) }
}
