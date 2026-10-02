import { describe, expect, it } from 'vitest'

import { accountHistory, compactOrder, compactPosition, HistoryInput } from '../src/account.js'
import { toTable, toolXml } from '../src/compact.js'
import { compactGreeks, compactMarketMetric, compactQuote, exchangesForSymbols, legQuote, marketStatus, nextOpenTime } from '../src/market.js'
import { compactWatchlist, manageWatchlist, WatchlistInput } from '../src/watchlists.js'
import { equity, fakeBroker, parseToolXml } from './fake-client.js'

const tsla = equity('TSLA')
const spx = { ...equity('SPX'), kind: 'Index' as const }

describe('compact output', () => {
  it('renders an empty table as No data', () => {
    expect(toTable([])).toBe('No data')
  })

  it('aligns a plain table with numeric columns right-aligned', () => {
    expect(toTable([{ sym: 'A', bid: '1.5' }, { sym: 'BBB', bid: '10' }])).toBe('sym  bid\nA    1.5\nBBB   10')
  })

  it('wraps tool output as escaped compact JSON', () => {
    const xml = toolXml('order', { note: 'a<b & c' })
    expect(xml).toBe('<order>{"note":"a&lt;b &amp; c"}</order>')
    expect(parseToolXml(xml, 'order')).toEqual({ note: 'a<b & c' })
  })

  it('keeps only actionable quote fields', () => {
    const row = { symbol: 'TSLA', bid: '10.10', ask: '10.30', 'bid-size': '12', 'ask-size': 9, last: '10.2', 'updated-at': 'x' }
    expect(compactQuote(tsla, row)).toEqual({ sym: 'TSLA', bid: '10.1', ask: '10.3', mid: '10.2', bid_sz: '12', ask_sz: '9' })
  })

  it('refuses a one-sided quote instead of using the last price', () => {
    expect(() => compactQuote(tsla, { symbol: 'TSLA', bid: '10.10', last: '10.2' })).toThrow('ask price')
  })

  it('refuses a crossed or negative quote', () => {
    expect(() => legQuote({ bid: '1.20', ask: '1.00' }, 'AAPL')).toThrow('Crossed quote')
    expect(() => legQuote({ bid: '-1', ask: '1.00' }, 'AAPL')).toThrow('cannot be negative')
  })

  it('reports an index without a two-sided market by its last price', () => {
    expect(compactQuote(spx, { symbol: 'SPX', last: '6500.25' })).toEqual({ sym: 'SPX', last: '6500.25' })
  })

  it('omits stream metadata from Greeks', () => {
    expect(compactGreeks({ eventSymbol: '.TSLA260116C300', price: 10.2, volatility: 0.54321, delta: 0.45, theta: -0.03 })).toEqual({
      sym: '.TSLA260116C300',
      price: 10.2,
      iv: 0.54321,
      delta: 0.45,
      theta: -0.03,
    })
  })

  it('keeps market metrics flat', () => {
    const row = compactMarketMetric({
      symbol: 'TSLA',
      'implied-volatility-index-rank': '0.21',
      'implied-volatility-30-day': '0.550',
      'option-expiration-implied-volatilities': [{ large: 'surface' }],
      beta: '1.2',
      earnings: { 'expected-report-date': '2026-01-20' },
    })
    expect(row).toEqual({ symbol: 'TSLA', iv_rank: '0.21', iv30: '0.55', beta: '1.2', earnings: '2026-01-20' })
    expect(() => compactMarketMetric({ beta: '1' })).toThrow('missing symbol')
  })

  it('compacts positions with signed realized gains', () => {
    const row = compactPosition({
      symbol: 'TSLA',
      'instrument-type': 'Equity Option',
      'underlying-symbol': 'TSLA',
      quantity: 2,
      'quantity-direction': 'Long',
      'average-open-price': '10.50',
      'mark-price': '11.00',
      'realized-day-gain': '0.0',
      'realized-today': '25.5',
      'realized-today-effect': 'Debit',
      'expires-at': '2026-01-16T21:15:00.000+00:00',
    })
    expect(row).toEqual({
      symbol: 'TSLA',
      type: 'Equity Option',
      underlying: 'TSLA',
      qty: '2',
      dir: 'Long',
      avg_open: '10.5',
      mark: '11',
      today: '-25.5',
      expires: '2026-01-16T21:15:00.000+00:00',
    })
  })

  it('renders epoch-millisecond timestamps as ISO time', () => {
    const order = compactOrder({ id: 1, 'updated-at': 1790775524940, legs: [{ action: 'Buy to Open', quantity: 1, symbol: 'X' }] })
    expect(order.updated_at).toBe('2026-09-30T13:38:44.940Z')
  })

  it('pages history from a New York start date', async () => {
    const { broker, client } = fakeBroker((call) => (call.path.endsWith('/transactions') ? { items: [] } : undefined))
    await accountHistory(broker, { type: 'transactions', page_offset: 0, limit: 25 }, new Date('2026-10-02T02:00:00Z'))
    expect(client.calls.at(-1)?.query).toMatchObject({ 'start-date': '2026-07-03', 'per-page': 25, sort: 'Desc' })
    expect(HistoryInput.safeParse({ type: 'orders', transaction_type: 'Trade' }).error?.issues[0]?.message).toBe(
      'transaction_type is only valid for transaction history',
    )
  })
})

describe('watchlists', () => {
  const tech = {
    name: 'tech',
    'group-name': 'main',
    'watchlist-entries': [
      { symbol: 'TSLA', 'instrument-type': 'Equity' },
      { symbol: 'NVDA', 'instrument-type': 'Equity' },
    ],
  }

  it('omits symbols until a named fetch', () => {
    expect(compactWatchlist(tech, false)).toEqual({ name: 'tech', group: 'main', symbol_count: 2 })
    expect(compactWatchlist(tech, true).symbols).toEqual(['TSLA:Equity', 'NVDA:Equity'])
    expect(() => compactWatchlist({ name: 'x', 'watchlist-entries': [{ symbol: 'TSLA' }] }, true)).toThrow('instrument-type')
  })

  it('appends to an existing watchlist and creates a missing one', async () => {
    const { broker, client } = fakeBroker((call) => {
      if (call.path === '/watchlists' && call.method === 'GET') return { items: [tech] }
      if (call.method === 'PUT' || call.method === 'POST') return {}
      return undefined
    })
    const add = { action: 'add' as const, watchlist_type: 'private' as const, symbols: [{ symbol: 'AMD', instrument_type: 'Equity' as const }] }
    expect(await manageWatchlist(broker, { ...add, name: 'tech' })).toEqual({ status: 'added', name: 'tech', symbols_added: 1 })
    expect(client.calls.at(-1)).toMatchObject({ method: 'PUT', path: '/watchlists/tech' })
    expect((client.calls.at(-1)?.body as { 'watchlist-entries': unknown[] })['watchlist-entries']).toHaveLength(3)
    expect(await manageWatchlist(broker, { ...add, name: 'new' })).toEqual({ status: 'created', name: 'new', symbols_added: 1 })
    expect(client.calls.at(-1)).toMatchObject({ method: 'POST', path: '/watchlists' })
  })

  it('refuses to change a public watchlist', () => {
    expect(WatchlistInput.safeParse({ action: 'delete', watchlist_type: 'public', name: 'x' }).error?.issues[0]?.message).toBe(
      "action='delete' is supported only for private watchlists",
    )
  })
})

describe('market status', () => {
  it('maps streamer symbols to exchanges', () => {
    expect([...exchangesForSymbols(['AAPL', '/ESZ6', './ESZ26C6000:XCME', '/VXZ6', './VXZ26C20:XCBF'])].sort()).toEqual([
      'CFE',
      'CME',
      'Equity',
    ])
  })

  it('finds the next open across session states', () => {
    const now = new Date('2026-10-02T12:00:00Z')
    const next = '2026-10-05T13:30:00Z'
    expect(nextOpenTime({ exchange: 'Equity', status: 'Pre-market', openAt: '2026-10-02T13:30:00Z' }, now)).toBe('2026-10-02T13:30:00Z')
    expect(nextOpenTime({ exchange: 'Equity', status: 'Extended', nextOpenAt: next }, now)).toBe(next)
    expect(
      nextOpenTime({ exchange: 'Equity', status: 'Closed', openAt: '2026-10-01T13:30:00Z', closeAt: '2026-10-01T20:00:00Z', nextOpenAt: next }, now),
    ).toBe(next)
    expect(nextOpenTime({ exchange: 'Equity', status: 'Open' }, now)).toBeUndefined()
  })

  it('reports holidays by the New York date', async () => {
    const { broker } = fakeBroker((call) => {
      if (call.path === '/market-time/sessions/current') {
        return { items: [{ 'instrument-collection': 'Equity', state: 'Closed', 'next-session': { 'open-at': '2026-12-28T14:30:00Z' } }] }
      }
      if (call.path === '/market-time/equities/holidays') return { 'market-holidays': ['2026-12-25'], 'market-half-days': [] }
      return undefined
    })
    // 02:00 UTC on the 26th is still the 25th in New York.
    const status = await marketStatus(broker, { exchanges: ['Equity'] }, new Date('2026-12-26T02:00:00Z'))
    expect(status.current_time_nyc).toBe('2026-12-25T21:00:00-05:00')
    expect(status.exchanges).toEqual([{ exchange: 'Equity', status: 'Closed', is_holiday: true }])
  })
})
