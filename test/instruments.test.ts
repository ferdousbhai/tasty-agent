import { describe, expect, it } from 'vitest'

import { selectAccount } from '../src/broker.js'
import { ChainCache, InstrumentSpecSchema, OptionSpecSchema, OrderLegSchema, resolveInstrumentKind } from '../src/instruments.js'
import { fakeBroker } from './fake-client.js'

const esChain = {
  futures: [{ symbol: '/ESZ6' }],
  'option-chains': [
    {
      'underlying-symbol': '/ES',
      'root-symbol': '/ES',
      expirations: [
        {
          'expiration-date': '2026-12-18',
          'tick-sizes': [{ value: '0.05', threshold: '5' }, { value: '0.25' }],
          strikes: [
            { 'strike-price': '6000.0', call: './ESZ6 ESZ6 261218C6000', 'call-streamer-symbol': './ESZ26C6000:XCME', put: './ESZ6 ESZ6 261218P6000', 'put-streamer-symbol': './ESZ26P6000:XCME' },
            { 'strike-price': '6050.0', call: './ESZ6 ESZ6 261218C6050', 'call-streamer-symbol': './ESZ26C6050:XCME', put: './ESZ6 ESZ6 261218P6050', 'put-streamer-symbol': './ESZ26P6050:XCME' },
          ],
        },
      ],
    },
  ],
}

describe('instrument specs', () => {
  it('rejects a partial option identity', () => {
    expect(InstrumentSpecSchema.safeParse({ symbol: 'AAPL', option_type: 'C' }).success).toBe(false)
  })

  it('rejects an explicit type combined with option fields', () => {
    const parsed = InstrumentSpecSchema.safeParse({
      symbol: 'SPX',
      instrument_type: 'Index',
      option_type: 'C',
      strike_price: 5000,
      expiration_date: '2026-12-18',
    })
    expect(parsed.success).toBe(false)
  })

  it.each([
    [{ symbol: 'AAPL', action: 'Buy to Open' }, true],
    [{ symbol: '/ESZ6', action: 'Buy' }, true],
    [{ symbol: '/ES', action: 'Sell to Open', option_type: 'C', strike_price: 6000, expiration_date: '2026-12-18' }, true],
    [{ symbol: 'AAPL', action: 'Buy' }, false],
    [{ symbol: '/ESZ6', action: 'Buy to Open' }, false],
  ])('validates the action for the instrument: %j', (leg, valid) => {
    expect(OrderLegSchema.safeParse(leg).success).toBe(valid)
  })

  it('infers the instrument kind', () => {
    expect(resolveInstrumentKind({ symbol: 'AAPL' })).toBe('Equity')
    expect(resolveInstrumentKind({ symbol: '/ESZ6' })).toBe('Future')
    expect(resolveInstrumentKind({ symbol: 'AAPL', option_type: 'P' })).toBe('Equity Option')
    expect(resolveInstrumentKind({ symbol: '/ES', option_type: 'C' })).toBe('Future Option')
    expect(resolveInstrumentKind({ symbol: 'SPX', instrument_type: 'Index' })).toBe('Index')
  })

  it('validates expiration dates and strikes', () => {
    const option = { symbol: 'AAPL', option_type: 'C', strike_price: 150.5, expiration_date: '2026-01-16' }
    expect(OptionSpecSchema.safeParse(option).success).toBe(true)
    for (const invalid of [{ expiration_date: '01/16/2026' }, { expiration_date: '2026-02-30' }, { strike_price: 0 }, { strike_price: -1 }]) {
      expect(OptionSpecSchema.safeParse({ ...option, ...invalid }).success).toBe(false)
    }
  })
})

describe('instrument resolution', () => {
  it('resolves a futures option from the nested chain with its tick schedule', async () => {
    const { broker } = fakeBroker((call) => (call.path === '/futures-option-chains/ES/nested' ? esChain : undefined))
    const [detail] = await broker.instruments.resolveAll([{ symbol: '/ES', option_type: 'C', strike_price: 6000, expiration_date: '2026-12-18' }])
    expect(detail).toMatchObject({ kind: 'Future Option', symbol: './ESZ6 ESZ6 261218C6000', streamerSymbol: './ESZ26C6000:XCME' })
    expect(detail!.tick).toEqual({ tiers: [expect.anything(), expect.anything()] })
  })

  it('lists available strikes for a missing contract', async () => {
    const { broker } = fakeBroker((call) => (call.path === '/futures-option-chains/ES/nested' ? esChain : undefined))
    await expect(
      broker.instruments.resolveAll([{ symbol: '/ES', option_type: 'C', strike_price: 6025, expiration_date: '2026-12-18' }]),
    ).rejects.toThrow('Futures option not found: /ES 2026-12-18 C 6025. Available strikes: [6000, 6050]')
  })

  it('lists available expirations for a missing date', async () => {
    const { broker } = fakeBroker((call) => (call.path === '/futures-option-chains/ES/nested' ? esChain : undefined))
    await expect(
      broker.instruments.resolveAll([{ symbol: '/ES', option_type: 'C', strike_price: 6000, expiration_date: '2026-12-19' }]),
    ).rejects.toThrow('No futures options found for /ES expiration 2026-12-19. Available: [2026-12-18]')
  })

  it('reads each chain once, even for concurrent legs', async () => {
    const { broker, client } = fakeBroker((call) => (call.path === '/futures-option-chains/ES/nested' ? esChain : undefined))
    const spec = { symbol: '/ES', option_type: 'C' as const, strike_price: 6000, expiration_date: '2026-12-18' }
    // Two legs at once, then again later: one download serves all three.
    await broker.instruments.resolveAll([spec, { ...spec, option_type: 'P' as const }])
    await broker.instruments.resolveAll([spec])
    expect(client.calls.filter((call) => call.path.includes('option-chains'))).toHaveLength(1)
  })

  it('expires cached chains after their lifetime', () => {
    let now = 0
    const cache = new ChainCache(undefined, 1000, () => now)
    cache.set('k', [])
    expect(cache.get('k')).toEqual([])
    now = 1000
    expect(cache.get('k')).toBeUndefined()
  })
})

describe('account selection', () => {
  it('requires TASTYTRADE_ACCOUNT_ID when several accounts are exposed', () => {
    expect(() => selectAccount(['A', 'B'], undefined)).toThrow('TASTYTRADE_ACCOUNT_ID is required')
  })

  it('selects the only account without configuration', () => {
    expect(selectAccount(['A'], undefined)).toBe('A')
  })

  it('rejects an unknown configured account', () => {
    expect(() => selectAccount(['A'], 'B')).toThrow("Account 'B' not found. Available: [A]")
  })
})
