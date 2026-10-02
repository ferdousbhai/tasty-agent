import { describe, expect, it } from 'vitest'

import { placeOrder, replaceOrder } from '../src/orders.js'
import { TastytradeOutcomeUnknownError } from '../src/tastytrade/errors.js'
import type { JsonObject, JsonValue } from '../src/tastytrade/json.js'
import { fakeBroker, type Call } from './fake-client.js'

const MSFT_CALL = 'MSFT  280121C00450000'

const msftChain = {
  items: [
    {
      'underlying-symbol': 'MSFT',
      'root-symbol': 'MSFT',
      'shares-per-contract': 100,
      expirations: [
        {
          'expiration-date': '2028-01-21',
          strikes: [
            { 'strike-price': '450.0', call: MSFT_CALL, 'call-streamer-symbol': '.MSFT280121C450', put: 'MSFT  280121P00450000', 'put-streamer-symbol': '.MSFT280121P450' },
          ],
        },
      ],
    },
  ],
}

function envelope(order: JsonObject, extra: JsonObject = {}): JsonObject {
  return {
    order: { id: 12345, status: 'Received', 'order-type': 'Limit', 'time-in-force': 'Day', ...order },
    'buying-power-effect': { 'change-in-buying-power': '44345.0', 'change-in-buying-power-effect': 'Debit' },
    'fee-calculation': { commission: '7.0', 'commission-effect': 'Debit', 'total-fees': '0' },
    ...extra,
  }
}

function msftRoutes(overrides: { dryRun?: JsonValue; live?: (call: Call) => JsonValue } = {}) {
  return (call: Call): JsonValue | undefined => {
    if (call.path === '/option-chains/MSFT/nested') return msftChain
    if (call.path === '/instruments/equities') {
      return { items: [{ symbol: 'MSFT', 'option-tick-sizes': [{ value: '0.01', threshold: '3' }, { value: '0.05' }] }] }
    }
    if (call.path === '/market-data/by-type') return { items: [{ symbol: MSFT_CALL, bid: '61.65', ask: '65.0' }] }
    if (call.path === '/accounts/5WT00001/orders/dry-run') {
      return overrides.dryRun ?? envelope({ price: '63.35', 'price-effect': 'Debit', legs: (call.body as JsonObject).legs! })
    }
    if (call.path === '/accounts/5WT00001/orders' && call.method === 'POST') {
      return overrides.live?.(call) ?? envelope({ price: '63.35', 'price-effect': 'Debit', legs: (call.body as JsonObject).legs! })
    }
    return undefined
  }
}

const msftLeg = { symbol: 'MSFT', action: 'Buy to Open' as const, quantity: 1, option_type: 'C' as const, strike_price: 450, expiration_date: '2028-01-21' }

describe('place_order', () => {
  it('sizes from target value, prices at the tick-aligned mid, and previews without submitting', async () => {
    const { broker, client } = fakeBroker(msftRoutes())
    const result = await placeOrder(broker, { legs: [msftLeg], target_value: 50000, time_in_force: 'Day', dry_run: true })

    expect(result.sizing).toEqual({ target_value: '50000', unit_value: '6335', quantity: 7, estimated_value: '44345' })
    expect(result.pricing).toEqual({
      limit: '-63.35',
      market: 'natural=-$65.00, mid=-$63.32, passive=-$61.65, spread=$3.35, tick=$0.05',
    })
    expect(result.bp_effect).toEqual({ change_in_buying_power: '-44345' })
    expect(result.fees).toEqual({ commission: '-7' })
    const dryRun = client.calls.find((call) => call.path.endsWith('/orders/dry-run'))!
    expect(dryRun.body).toEqual({
      'time-in-force': 'Day',
      'order-type': 'Limit',
      price: '63.35',
      'price-effect': 'Debit',
      legs: [{ 'instrument-type': 'Equity Option', symbol: MSFT_CALL, quantity: 7, action: 'Buy to Open' }],
    })
    expect(client.calls.some((call) => call.path === '/accounts/5WT00001/orders')).toBe(false)
  })

  it('submits after a clean dry run', async () => {
    const { broker, client } = fakeBroker(msftRoutes())
    const result = await placeOrder(broker, { legs: [msftLeg], time_in_force: 'Day', dry_run: false })
    expect((result.order as JsonObject).id).toBe(12345)
    expect(client.calls.filter((call) => call.method === 'POST').map((call) => call.path)).toEqual([
      '/accounts/5WT00001/orders/dry-run',
      '/accounts/5WT00001/orders',
    ])
  })

  it('refuses to submit when the dry run returns any warning', async () => {
    const dryRun = envelope(
      { price: '63.35', 'price-effect': 'Debit', legs: [{ action: 'Buy to Open', quantity: 1, symbol: MSFT_CALL }] },
      { warnings: [{ code: 'tif_next_valid_session', message: 'Order will be routed next session' }] },
    )
    const { broker, client } = fakeBroker(msftRoutes({ dryRun }))
    await expect(placeOrder(broker, { legs: [msftLeg], time_in_force: 'Day', dry_run: false })).rejects.toThrow(
      'Tastytrade returned dry-run warnings, so the order was not submitted: tif_next_valid_session: Order will be routed next session',
    )
    expect(client.calls.some((call) => call.path === '/accounts/5WT00001/orders')).toBe(false)
  })

  it('flags a dry-run preview that live placement would refuse', async () => {
    const dryRun = envelope(
      { price: '63.35', 'price-effect': 'Debit', legs: [{ action: 'Buy to Open', quantity: 1, symbol: MSFT_CALL }] },
      { warnings: [{ code: 'w', message: 'careful' }] },
    )
    const { broker } = fakeBroker(msftRoutes({ dryRun }))
    const result = await placeOrder(broker, { legs: [msftLeg], time_in_force: 'Day', dry_run: true })
    expect(result.warnings).toEqual(['w: careful'])
    expect(result.blocked).toContain('would be refused')
  })

  it('reports broker errors in the dry run as a rejection', async () => {
    const { broker } = fakeBroker(msftRoutes({ dryRun: { errors: [{ code: 'invalid-price', message: 'Price is off tick' }] } }))
    await expect(placeOrder(broker, { legs: [msftLeg], time_in_force: 'Day', dry_run: true })).rejects.toThrow(
      'Broker rejected order: invalid-price: Price is off tick',
    )
  })

  it('reports an accepted submission with an unreadable response as an unknown outcome', async () => {
    const { broker } = fakeBroker(msftRoutes({ live: () => ({ order: { id: 1 } }) }))
    const error = await placeOrder(broker, { legs: [msftLeg], time_in_force: 'Day', dry_run: false }).catch((caught: unknown) => caught)
    expect(error).toBeInstanceOf(TastytradeOutcomeUnknownError)
    expect((error as Error).message).toContain('Check list_orders')
  })

  it('refuses orders on an index', async () => {
    const { broker } = fakeBroker((call) =>
      call.path === '/instruments/equities' ? { items: [{ symbol: 'SPX', 'streamer-symbol': 'SPX', 'is-index': true }] } : undefined,
    )
    await expect(
      placeOrder(broker, { legs: [{ symbol: 'SPX', action: 'Buy to Open', quantity: 1 }], time_in_force: 'Day', dry_run: true }),
    ).rejects.toThrow('Cannot place orders for index symbol')
  })
})

describe('replace_order', () => {
  it('reprices a live order at the current mid after a clean dry run', async () => {
    const live: JsonObject = {
      id: 777,
      status: 'Live',
      'underlying-symbol': 'AAPL',
      'time-in-force': 'Day',
      'order-type': 'Limit',
      price: '1.3',
      'price-effect': 'Debit',
      legs: [{ 'instrument-type': 'Equity', symbol: 'AAPL', action: 'Buy to Open', quantity: 100 }],
    }
    const { broker, client } = fakeBroker((call): JsonValue | undefined => {
      if (call.path === '/accounts/5WT00001/orders/live') return { items: [live] }
      if (call.path === '/instruments/equities') return { items: [{ symbol: 'AAPL', 'tick-sizes': [{ value: '0.01' }] }] }
      if (call.path === '/market-data/by-type') return { items: [{ symbol: 'AAPL', bid: '1.00', ask: '1.20' }] }
      if (call.path === '/accounts/5WT00001/orders/777/dry-run') return envelope({ ...live, price: '1.1' })
      if (call.path === '/accounts/5WT00001/orders/777' && call.method === 'PUT') return { ...live, id: 778, price: '1.1' }
      return undefined
    })
    const result = await replaceOrder(broker, { order_id: '777' })
    expect(result.order).toMatchObject({ id: 778, price: '-1.1' })
    expect(client.calls.find((call) => call.method === 'PUT')!.body).toEqual({
      'time-in-force': 'Day',
      'order-type': 'Limit',
      price: '1.1',
      'price-effect': 'Debit',
    })
  })

  it('refuses an order id that is not live', async () => {
    const { broker } = fakeBroker((call) => (call.path.endsWith('/orders/live') ? { items: [] } : undefined))
    await expect(replaceOrder(broker, { order_id: '1' })).rejects.toThrow('Order 1 not found in live orders')
  })
})
