const BALANCES_AND_POSITIONS_VERSION = '20240501'
const INSTRUMENTS_VERSION = '20250715'
const ORDERS_VERSION = '20260427'

/** Tastytrade versions endpoint families independently; unversioned APIs omit the header. */
export function tastytradeApiVersion(path: string): string | undefined {
  const pathname = path.split('?', 1)[0]!
  if (/^\/accounts\/[^/]+\/(?:balances(?:\/|$)|balance-snapshots(?:\/|$)|positions(?:\/|$))/.test(pathname)) {
    return BALANCES_AND_POSITIONS_VERSION
  }
  if (/^\/(?:instruments|option-chains|futures-option-chains)(?:\/|$)/.test(pathname)) {
    return INSTRUMENTS_VERSION
  }
  if (/^\/(?:accounts\/[^/]+\/(?:orders|complex-orders)|customers\/[^/]+\/orders)(?:\/|$)/.test(pathname)) {
    return ORDERS_VERSION
  }
  return undefined
}
