import { Decimal } from 'decimal.js'

import { isJsonObject, jsonDecimal, jsonText, type JsonValue } from './json.js'

// A real schedule has a handful of tiers; a wider fan-out is anomalous data, not a schedule.
const MAX_TICK_TIERS = 50

export type TickSize = {
  /** null marks the unbounded tier (the provider writes it as an absent threshold or "Infinity"). */
  threshold: Decimal | null
  value: Decimal
}

/**
 * How a schedule's thresholds read.
 *
 * - `floor` (equity `tick-sizes`): a threshold is the lowest price its tier applies to, e.g.
 *   `[{value: 0.0001}, {threshold: 1, value: 0.01}]` is sub-penny below $1 and a cent from $1 up.
 * - `ceiling` (option `option-tick-sizes`, futures-option chain `tick-sizes`): a threshold is the
 *   exclusive upper bound of its tier, e.g. `[{threshold: 3, value: 0.05}, {threshold: Infinity,
 *   value: 0.10}]` is a nickel below $3 and a dime from $3 up.
 */
export type TickSchedule = 'floor' | 'ceiling'

/** Normalizes the provider's two forms (one object or an array of them); absent data is `[]`. */
export function parseTickSizes(value: JsonValue | undefined, label: string): TickSize[] {
  if (value === undefined || value === null) return []
  const rows = Array.isArray(value) ? value : [value]
  if (rows.length > MAX_TICK_TIERS) throw new Error(`${label}: too many tick-size tiers`)
  return rows.map((row) => {
    if (!isJsonObject(row)) throw new Error(`${label}: tick-size tier is not an object`)
    const tick = jsonDecimal(row.value)
    if (!tick || tick.lte(0)) throw new Error(`${label}: invalid tick-size value`)
    const rawThreshold = jsonText(row.threshold)?.trim()
    if (rawThreshold === undefined || rawThreshold.toLowerCase() === 'infinity') return { threshold: null, value: tick }
    const threshold = jsonDecimal(rawThreshold)
    if (!threshold || threshold.lte(0)) throw new Error(`${label}: invalid tick-size threshold`)
    return { threshold, value: tick }
  })
}

/** The tick that applies at `price`, or an error when the schedule does not decide it unambiguously. */
export function tickSizeAt(tiers: readonly TickSize[], price: Decimal, schedule: TickSchedule, label: string): Decimal {
  if (!tiers.length) throw new Error(`${label}: no tick-size tiers`)
  const absolute = price.abs()
  const unbounded = tiers.filter((tier) => tier.threshold === null)
  const bounded = tiers
    .filter((tier): tier is { threshold: Decimal; value: Decimal } => tier.threshold !== null)
    .sort((left, right) => left.threshold.comparedTo(right.threshold))
  const distinct = new Set(bounded.map((tier) => tier.threshold.toString()))
  if (unbounded.length > 1 || distinct.size !== bounded.length) {
    throw new Error(`${label}: ambiguous tick-size tiers`)
  }

  const match =
    schedule === 'floor'
      ? [...bounded].reverse().find((tier) => absolute.gte(tier.threshold))
      : bounded.find((tier) => absolute.lt(tier.threshold))
  const value = match?.value ?? unbounded[0]?.value
  if (!value) throw new Error(`${label}: no tick-size tier covers price ${absolute.toString()}`)
  return value
}
