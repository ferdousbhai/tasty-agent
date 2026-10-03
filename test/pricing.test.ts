import { Decimal } from 'decimal.js'
import { describe, expect, it } from 'vitest'

import type { InstrumentDetail } from '../src/instruments.js'
import { PlaceOrderInput } from '../src/orders.js'
import { buildOrderMarket, formatOrderMarket, resolveOrderPrice, sizeOrder, type LegQuote } from '../src/pricing.js'
import { equity, future, option } from './fake-client.js'

const d = (value: string | number) => new Decimal(value)
const quote = (bid: string, ask: string) => ({ bid: d(bid), ask: d(ask) })

/** Zips parallel instrument, leg, and quote lists into the market's leg records. */
function orderMarket(details: InstrumentDetail[], legs: { action: string; quantity: number }[], quotes: LegQuote[]) {
  return buildOrderMarket(legs.map((leg, index) => ({ ...leg, detail: details[index]!, quote: quotes[index]! })))
}

describe('order market', () => {
  it('rejects an empty order', () => {
    expect(() => orderMarket([], [], [])).toThrow('At least one order leg')
  })

  it('prices a buy at the signed mid of the exact instrument quote', () => {
    const market = orderMarket([equity('AAPL')], [{ action: 'Buy to Open', quantity: 1 }], [quote('1.00', '1.20')])
    expect(market.naturalPrice.toString()).toBe('-1.2')
    expect(market.passivePrice.toString()).toBe('-1')
    expect(market.midPrice.toString()).toBe('-1.1')
    expect(resolveOrderPrice(market)).toEqual({ price: d('-1.1'), warnings: [] })
  })

  it('prices per share or contract, not per total quantity', () => {
    const market = orderMarket([equity('AAPL')], [{ action: 'Buy to Open', quantity: 17 }], [quote('1.00', '1.20')])
    expect(market.naturalPrice.toString()).toBe('-1.2')
    expect(resolveOrderPrice(market).price.toString()).toBe('-1.1')
  })

  it('normalizes equal leg quantities of a spread to one unit', () => {
    const market = orderMarket(
      [equity('AAPL_150C'), equity('AAPL_155C')],
      [
        { action: 'Buy to Open', quantity: 17 },
        { action: 'Sell to Open', quantity: 17 },
      ],
      [quote('1.00', '1.20'), quote('0.50', '0.60')],
    )
    expect(market.naturalPrice.toString()).toBe('-0.7')
    expect(market.passivePrice.toString()).toBe('-0.4')
    expect(resolveOrderPrice(market)).toEqual({ price: d('-0.55'), warnings: [] })
  })


  it('aligns a futures price to the contract tick', () => {
    const market = orderMarket([future('/ESM26')], [{ action: 'Buy', quantity: 1 }], [quote('100.00', '100.50')])
    expect(market.tickSize.toString()).toBe('0.25')
    expect(resolveOrderPrice(market)).toEqual({ price: d('-100.25'), warnings: [] })
  })

  // Tastytrade's own payloads: SPY's `tick-sizes` and SPX's `option-tick-sizes`.
  const equityTiers = [{ threshold: '1.0', value: '0.0001' }, { value: '0.01' }]
  const optionTiers = [{ threshold: '3', value: '0.05' }, { threshold: 'Infinity', value: '0.10' }]
  it.each([
    ['below it', equityTiers, '0.1234', '0.1236', '0.0001'],
    ['above it', equityTiers, '189.991', '190.009', '0.01'],
    ['below it, with an Infinity top tier', optionTiers, '2.80', '2.90', '0.05'],
    ['exactly at it', optionTiers, '2.90', '3.10', '0.1'],
  ])('reads a tick threshold as an exclusive upper bound: mid %s', (_, tiers, bid, ask, tick) => {
    const market = orderMarket([equity('X', tiers)], [{ action: 'Buy to Open', quantity: 1 }], [quote(bid, ask)])
    expect(market.tickSize.toString()).toBe(tick)
  })

  it('refuses a schedule without an unbounded top tier', () => {
    const incomplete = equity('X', [{ threshold: '3', value: '0.05' }])
    expect(() => orderMarket([incomplete], [{ action: 'Buy to Open', quantity: 1 }], [quote('1.00', '1.10')])).toThrow(
      'no unbounded top tier',
    )
  })

  it('prices on a sub-penny tick', () => {
    const market = orderMarket([equity('PENNY', equityTiers)], [{ action: 'Buy to Open', quantity: 100 }], [quote('0.1234', '0.1236')])
    expect(resolveOrderPrice(market)).toEqual({ price: d('-0.1235'), warnings: [] })
  })

  it('rounds a one-tick-wide option market to the nearest tick and warns', () => {
    const detail = option('.TQQQ270115C65', [{ value: '0.01', threshold: '3' }, { value: '0.05' }])
    const market = orderMarket([detail], [{ action: 'Buy to Open', quantity: 1 }], [quote('19.67', '19.69')])
    const { price, warnings } = resolveOrderPrice(market)
    expect(market.midPrice.toString()).toBe('-19.68')
    expect(market.tickSize.toString()).toBe('0.05')
    expect(price.toString()).toBe('-19.7')
    expect(warnings).toHaveLength(1)
    expect(warnings[0]).toContain('nearest valid tick')
  })

  it('rounds the mid to the nearest option tick', () => {
    const market = orderMarket(
      [option('.AAPL261218C150', [{ value: '0.05' }])],
      [{ action: 'Buy to Open', quantity: 1 }],
      [quote('1.11', '1.13')],
    )
    const { price, warnings } = resolveOrderPrice(market)
    expect(market.midPrice.toString()).toBe('-1.12')
    expect(price.toString()).toBe('-1.1')
    expect(warnings[0]).toContain('nearest valid tick')
  })

  it('refuses to price without broker tick sizes', () => {
    expect(() =>
      orderMarket([option('.AAPL261218C150')], [{ action: 'Buy to Open', quantity: 1 }], [quote('1.11', '1.13')]),
    ).toThrow('Missing broker tick sizes')
    expect(() =>
      orderMarket([equity('AAPL', [])], [{ action: 'Buy to Open', quantity: 1 }], [quote('1.11', '1.13')]),
    ).toThrow('Missing broker tick sizes')
  })

  it('formats the market with signed money', () => {
    const market = orderMarket(
      [option('.MSFT280121C450', [{ value: '0.01', threshold: '3' }, { value: '0.05' }])],
      [{ action: 'Buy to Open', quantity: 1 }],
      [quote('61.65', '65.00')],
    )
    expect(resolveOrderPrice(market).price.toString()).toBe('-63.35')
    expect(formatOrderMarket(market)).toBe(
      'natural=-$65.00, mid=-$63.32, passive=-$61.65, spread=$3.35, tick=$0.05',
    )
  })
})

describe('target value sizing', () => {
  it('sizes option contracts by shares per contract', () => {
    expect(sizeOrder([option('TSLA_300C')], d(-10), d(50000))).toEqual({
      targetValue: d(50000),
      unitValue: d(1000),
      quantity: 50,
      estimatedValue: d(50000),
    })
  })

  it('sizes equity shares', () => {
    const sizing = sizeOrder([equity('TSLA')], d(-250), d(50000))
    expect(sizing.quantity).toBe(200)
    expect(sizing.unitValue.toString()).toBe('250')
  })

  it('sizes a multi-leg ratio by its net unit price', () => {
    const sizing = sizeOrder([option('TSLA_300C'), option('TSLA_320C')], d(-5), d(50000))
    expect(sizing.quantity).toBe(100)
    expect(sizing.estimatedValue.toString()).toBe('50000')
  })

  it('refuses a budget smaller than one unit', () => {
    expect(() => sizeOrder([option('X')], d(-10), d(500))).toThrow('too small for one order unit')
  })

  it('accepts sizing only for a reduced ratio of equity and equity-option legs', () => {
    const leg = { symbol: 'TSLA', action: 'Buy to Open', quantity: 17 }
    const issues = (legs: object[]) => PlaceOrderInput.safeParse({ legs, target_value: 50000 }).error?.issues.map((issue) => issue.message)
    expect(issues([leg])?.[0]).toContain('smallest whole-number ratio')
    expect(issues([{ symbol: '/ESZ6', action: 'Buy', quantity: 1 }])?.[0]).toContain('equities and equity options only')
    expect(issues([{ ...leg, quantity: 1 }])).toBeUndefined()
  })
})
