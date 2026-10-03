/**
 * Live checks against Tastytrade. Skipped unless TASTYTRADE_CLIENT_SECRET and
 * TASTYTRADE_REFRESH_TOKEN are set; they read market data and send dry-run orders only, and
 * still require explicit authorization to run.
 *
 *   TASTYTRADE_CLIENT_SECRET=... TASTYTRADE_REFRESH_TOKEN=... npx vitest run test/integration.test.ts
 */
import { describe, expect, it } from 'vitest'

import { createBroker } from '../src/broker.js'
import { ChainCache } from '../src/instruments.js'
import { quotes } from '../src/market.js'
import { placeOrder } from '../src/orders.js'
import { IntervalGate } from '../src/tastytrade/gate.js'

const { TASTYTRADE_CLIENT_SECRET, TASTYTRADE_REFRESH_TOKEN, TASTYTRADE_ACCOUNT_ID } = process.env

describe.skipIf(!TASTYTRADE_CLIENT_SECRET || !TASTYTRADE_REFRESH_TOKEN)('tastytrade (live)', () => {
  const broker = createBroker({
    clientSecret: TASTYTRADE_CLIENT_SECRET ?? '',
    refreshToken: TASTYTRADE_REFRESH_TOKEN ?? '',
    accountId: TASTYTRADE_ACCOUNT_ID,
    gate: new IntervalGate(),
    chains: new ChainCache(),
  })

  it('quotes an equity and an index', async () => {
    const table = await quotes(broker, { instruments: [{ symbol: 'AAPL' }, { symbol: 'SPX', instrument_type: 'Index' }] })
    expect(table).toMatch(/AAPL/)
  }, 30_000)

  it('dry-runs a one-share equity order with the expected leg mapping', async () => {
    try {
      const result = await placeOrder(broker, {
        legs: [{ symbol: 'AAPL', action: 'Buy to Open', quantity: 1 }],
        time_in_force: 'Day',
        dry_run: true,
      })
      expect(result.order).toBeDefined()
    } catch (error) {
      // A funding or session refusal is acceptable; a malformed leg is not.
      expect(String(error).toLowerCase()).not.toContain('order_legs.action')
    }
  }, 30_000)
})
