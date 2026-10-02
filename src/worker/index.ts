import { createMcpHandler } from '@modelcontextprotocol/server'

import { createBroker, type BrokerMemo } from '../broker.js'
import { ChainCache } from '../instruments.js'
import { createServer } from '../server.js'
import { memoryTokenStore } from '../tastytrade/client.js'
import { SERVER_VERSION } from '../version.js'
import type { BrokerGate } from './broker-gate.js'

export { BrokerGate } from './broker-gate.js'

export interface Env {
  TASTYTRADE_CLIENT_SECRET: string
  TASTYTRADE_REFRESH_TOKEN: string
  TASTYTRADE_ACCOUNT_ID?: string
  TASTYTRADE_API_BASE?: string
  /** Callers must present `Authorization: Bearer <MCP_BEARER_TOKEN>`. */
  MCP_BEARER_TOKEN: string
  BROKER_GATE: DurableObjectNamespace<BrokerGate>
}

const MCP_PATH = '/mcp'

// Settled values only: a pending promise is request-scoped I/O and must not cross requests.
const tokenStore = memoryTokenStore()
// A smaller chain cache than Node's: an isolate has 128 MB in all.
const chains = new ChainCache(50_000)
const memo: BrokerMemo = {}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url)
    if (url.pathname !== MCP_PATH) {
      return new Response(`tasty-agent ${SERVER_VERSION}: MCP endpoint is ${MCP_PATH}\n`, { status: 404 })
    }
    if (!env.MCP_BEARER_TOKEN || !env.TASTYTRADE_CLIENT_SECRET || !env.TASTYTRADE_REFRESH_TOKEN) {
      return new Response(
        'Server is not configured: set the MCP_BEARER_TOKEN, TASTYTRADE_CLIENT_SECRET and TASTYTRADE_REFRESH_TOKEN secrets.\n',
        { status: 500 },
      )
    }
    if (!(await bearerMatches(request.headers.get('Authorization'), env.MCP_BEARER_TOKEN))) {
      return new Response('Unauthorized\n', { status: 401, headers: { 'WWW-Authenticate': 'Bearer' } })
    }

    const gate = env.BROKER_GATE.getByName('tastytrade')
    const broker = createBroker({
      clientSecret: env.TASTYTRADE_CLIENT_SECRET,
      refreshToken: env.TASTYTRADE_REFRESH_TOKEN,
      accountId: env.TASTYTRADE_ACCOUNT_ID,
      apiBase: env.TASTYTRADE_API_BASE,
      gate: { acquire: () => gate.acquire() },
      chains,
      tokenStore,
      memo,
    })
    return createMcpHandler(() => createServer(broker)).fetch(request)
  },
} satisfies ExportedHandler<Env>

async function bearerMatches(header: string | null, expected: string): Promise<boolean> {
  const presented = header?.match(/^Bearer\s+(.+)$/i)?.[1]?.trim()
  if (!presented) return false
  // Compare digests so the comparison is constant-time regardless of the presented length.
  const encoder = new TextEncoder()
  const [left, right] = await Promise.all([
    crypto.subtle.digest('SHA-256', encoder.encode(presented)),
    crypto.subtle.digest('SHA-256', encoder.encode(expected)),
  ])
  return crypto.subtle.timingSafeEqual(left, right)
}
