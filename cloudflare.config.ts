import { bindings, defineConfig, exports } from 'cf/config'

// Secrets (set with `npx cf workers secrets put <NAME>`, or in the dashboard):
//   TASTYTRADE_CLIENT_SECRET, TASTYTRADE_REFRESH_TOKEN, MCP_BEARER_TOKEN
// Optional: TASTYTRADE_ACCOUNT_ID (required when the grant exposes several accounts).
export default defineConfig({
  worker: {
    name: 'tasty-agent',
    compatibilityDate: '2026-09-01',
    entrypoint: './src/worker/index.ts',
    observability: { enabled: true },
    exports: {
      BrokerGate: exports.durableObject({ storage: 'sqlite' }),
    },
    env: {
      // One Durable Object instance holds the shared two-requests-per-second broker budget.
      BROKER_GATE: bindings.durableObject({ worker: 'tasty-agent', exportName: 'BrokerGate' }),
    },
  },
})
