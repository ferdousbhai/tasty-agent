#!/usr/bin/env node
import { serveStdio } from '@modelcontextprotocol/server/stdio'

import { createBroker } from './broker.js'
import { ChainCache } from './instruments.js'
import { createServer } from './server.js'
import { IntervalGate } from './tastytrade/gate.js'

const { TASTYTRADE_CLIENT_SECRET, TASTYTRADE_REFRESH_TOKEN, TASTYTRADE_ACCOUNT_ID, TASTYTRADE_API_BASE } = process.env

if (!TASTYTRADE_CLIENT_SECRET || !TASTYTRADE_REFRESH_TOKEN) {
  console.error(
    'Missing Tastytrade OAuth credentials. Set TASTYTRADE_CLIENT_SECRET and TASTYTRADE_REFRESH_TOKEN environment variables.',
  )
  process.exit(1)
}

const broker = createBroker({
  clientSecret: TASTYTRADE_CLIENT_SECRET,
  refreshToken: TASTYTRADE_REFRESH_TOKEN,
  accountId: TASTYTRADE_ACCOUNT_ID,
  apiBase: TASTYTRADE_API_BASE,
  // One gate for the whole process: every broker call shares the two-requests-per-second budget.
  gate: new IntervalGate(),
  chains: new ChainCache(),
})

serveStdio(() => createServer(broker))
