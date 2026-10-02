import { createMcpHandler } from '@modelcontextprotocol/server'
import { describe, expect, it } from 'vitest'

import { readFileSync } from 'node:fs'

import { createServer } from '../src/server.js'
import { SERVER_VERSION } from '../src/version.js'
import { fakeBroker, parseToolXml, type Route } from './fake-client.js'

/** One stateless 2025-era JSON-RPC exchange over the HTTP handler the Worker serves. */
async function rpc(route: Route, method: string, params: Record<string, unknown> = {}) {
  const { broker } = fakeBroker(route)
  const response = await createMcpHandler(() => createServer(broker)).fetch(
    new Request('http://localhost/mcp', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Accept: 'application/json, text/event-stream',
        'MCP-Protocol-Version': '2025-06-18',
      },
      body: JSON.stringify({ jsonrpc: '2.0', id: 1, method, params }),
    }),
  )
  expect(response.status).toBe(200)
  const data = (await response.text()).split('\n').find((line) => line.startsWith('data: '))
  return JSON.parse(data!.slice('data: '.length)).result
}

describe('mcp server', () => {
  it('reports the package version', () => {
    expect(SERVER_VERSION).toBe(JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8')).version)
  })

  it('advertises the tool surface', async () => {
    const { tools } = await rpc(() => undefined, 'tools/list')
    expect(tools.map((tool: { name: string }) => tool.name).sort()).toEqual([
      'account_overview',
      'cancel_order',
      'get_greeks',
      'get_history',
      'get_market_metrics',
      'get_quotes',
      'list_orders',
      'market_status',
      'place_order',
      'replace_order',
      'search_symbols',
      'watchlist',
    ])
  })

  it('wraps tool output in its tag', async () => {
    const result = await rpc(
      (call) => (call.path === '/accounts/5WT00001/orders/live' ? { items: [] } : undefined),
      'tools/call',
      { name: 'list_orders', arguments: {} },
    )
    expect(result.content[0].text).toBe('<orders>No data</orders>')
  })

  it('returns tool failures as error results', async () => {
    const result = await rpc(() => undefined, 'tools/call', { name: 'cancel_order', arguments: { order_id: '9' } })
    expect(result.isError).toBe(true)
    expect(result.content[0].text).toContain('Unexpected request: DELETE /accounts/5WT00001/orders/9')
  })

  it('serves account output through the handler', async () => {
    const result = await rpc(
      (call) => (call.path === '/accounts/5WT00001/balances' ? { 'net-liquidating-value': '1000.50', 'cash-balance': '0.0' } : undefined),
      'tools/call',
      { name: 'account_overview', arguments: { include: ['balances'] } },
    )
    expect(parseToolXml(result.content[0].text, 'account_overview')).toEqual({ balances: { net_liq: '1000.5' } })
  })

  it('offers the IV analysis prompt', async () => {
    const { prompts } = await rpc(() => undefined, 'prompts/list')
    expect(prompts.map((prompt: { name: string }) => prompt.name)).toEqual(['analyze_iv_opportunities'])
  })
})
