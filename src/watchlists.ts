import { z } from 'zod'

import type { Broker } from './broker.js'
import { compactRow, type Row } from './compact.js'
import { requestItems, requestObject } from './tastytrade/client.js'
import { isJsonObject, jsonObjects, jsonText, type JsonObject } from './tastytrade/json.js'

const WatchlistSymbolSchema = z.object({
  symbol: z.string().min(1).describe('Symbol, e.g. AAPL.'),
  instrument_type: z
    .enum(['Equity', 'Equity Option', 'Future', 'Future Option', 'Cryptocurrency', 'Warrant'])
    .describe('Tastytrade instrument type.'),
})

export const WatchlistInput = z
  .object({
    action: z.enum(['list', 'add', 'remove', 'delete']),
    watchlist_type: z.enum(['public', 'private']).default('private'),
    name: z.string().trim().min(1).optional().describe('Watchlist name; defaults to main for add/remove/delete.'),
    symbols: z
      .array(WatchlistSymbolSchema)
      .min(1)
      .optional()
      .describe('Required for add/remove; each needs symbol and tastytrade instrument_type.'),
  })
  .superRefine((input, ctx) => {
    if (input.action !== 'list' && input.watchlist_type !== 'private') {
      ctx.addIssue({ code: 'custom', message: `action='${input.action}' is supported only for private watchlists` })
    }
    if ((input.action === 'add' || input.action === 'remove') && !input.symbols) {
      ctx.addIssue({ code: 'custom', message: `'symbols' is required for action='${input.action}'` })
    }
  })

export function compactWatchlist(watchlist: JsonObject, includeSymbols: boolean): Row {
  const name = jsonText(watchlist.name)
  if (!name) throw new Error('Watchlist is missing name')
  const entries = watchlist['watchlist-entries']
  if (!Array.isArray(entries)) throw new Error(`Watchlist '${name}' is missing symbol entries`)
  const symbols = entries.map((entry, index) => {
    if (!isJsonObject(entry)) throw new Error(`Watchlist entry ${index} must be an object`)
    const symbol = jsonText(entry.symbol)
    const type = jsonText(entry['instrument-type'])
    if (!symbol) throw new Error(`Watchlist entry ${index} is missing symbol`)
    if (!type) throw new Error(`Watchlist entry ${index} is missing instrument-type`)
    return `${symbol}:${type}`
  })
  return compactRow({
    name,
    group: jsonText(watchlist['group-name']),
    symbol_count: symbols.length,
    symbols: includeSymbols ? symbols : undefined,
  })
}

const entryKey = (symbol: unknown, type: unknown) => `${String(symbol)}\u0000${String(type)}`

export async function manageWatchlist(broker: Broker, input: z.infer<typeof WatchlistInput>): Promise<Row | Row[]> {
  const client = broker.client

  if (input.action === 'list') {
    const base = input.watchlist_type === 'public' ? '/public-watchlists' : '/watchlists'
    if (input.name) return compactWatchlist(await requestObject(client, `${base}/${encodeURIComponent(input.name)}`), true)
    return (await requestItems(client, base)).map((watchlist) => compactWatchlist(watchlist, false))
  }

  const name = input.name ?? 'main'
  const path = `/watchlists/${encodeURIComponent(name)}`
  if (input.action === 'delete') {
    await client.request(path, { method: 'DELETE' })
    return { status: 'deleted', name }
  }

  const symbols = input.symbols!
  const entries = symbols.map((symbol) => ({ symbol: symbol.symbol, 'instrument-type': symbol.instrument_type }))
  if (input.action === 'add') {
    const existing = (await requestItems(client, '/watchlists')).find((watchlist) => watchlist.name === name)
    if (!existing) {
      await client.request('/watchlists', { method: 'POST', body: { name, 'group-name': 'main', 'watchlist-entries': entries } })
      return { status: 'created', name, symbols_added: symbols.length }
    }
    const current = jsonObjects(existing['watchlist-entries'])
    await client.request(path, { method: 'PUT', body: { ...existing, 'watchlist-entries': [...current, ...entries] } })
    return { status: 'added', name, symbols_added: symbols.length }
  }

  const watchlist = await requestObject(client, path)
  const removing = new Set(entries.map((entry) => entryKey(entry.symbol, entry['instrument-type'])))
  const kept = jsonObjects(watchlist['watchlist-entries']).filter(
    (entry) => !removing.has(entryKey(entry.symbol, entry['instrument-type'])),
  )
  await client.request(path, { method: 'PUT', body: { ...watchlist, 'watchlist-entries': kept } })
  return { status: 'removed', name, symbols_removed: symbols.length }
}
