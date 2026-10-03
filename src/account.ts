import { Decimal } from 'decimal.js'
import { z } from 'zod'

import type { Broker } from './broker.js'
import { compactRow, num, signedMoneyRow, toTable, type Row } from './compact.js'
import { nyDate } from './time.js'
import { requestItems } from './tastytrade/client.js'
import { jsonId, jsonObject, jsonText, jsonTime, signedField, type JsonObject, type JsonValue } from './tastytrade/json.js'

export const OverviewInput = z.object({
  include: z
    .array(z.enum(['balances', 'positions']))
    .min(1)
    .default(['balances', 'positions'])
    .describe('Sections to return; defaults to ["balances", "positions"].'),
})

export const HistoryInput = z
  .object({
    type: z.enum(['transactions', 'orders']).describe('transactions for trade/cash flows, orders for order history.'),
    days: z.number().int().nonnegative().optional().describe('Lookback; defaults to 90 for transactions, 7 for orders.'),
    underlying_symbol: z.string().optional().describe('Filter by underlying symbol.'),
    transaction_type: z.enum(['Trade', 'Money Movement']).optional().describe('Transactions only: Trade or Money Movement.'),
    page_offset: z.number().int().nonnegative().default(0).describe('Starting offset.'),
    limit: z.number().int().positive().default(25).describe('Page size.'),
  })
  .refine((query) => query.type === 'transactions' || query.transaction_type === undefined, {
    message: 'transaction_type is only valid for transaction history',
  })

const BALANCE_FIELDS = {
  'net-liquidating-value': 'net_liq',
  'cash-balance': 'cash',
  'cash-available-to-withdraw': 'cash_withdraw',
  'equity-buying-power': 'bp_equity',
  'derivative-buying-power': 'bp_deriv',
  'day-trading-buying-power': 'bp_day',
  'available-trading-funds': 'avail_funds',
  'maintenance-requirement': 'maint_req',
  'maintenance-excess': 'maint_excess',
  'futures-margin-requirement': 'fut_margin',
  'used-derivative-buying-power': 'used_deriv_bp',
} as const

function compactBalances(balance: JsonObject): Row {
  return compactRow({ ...signedMoneyRow(balance, BALANCE_FIELDS), updated_at: jsonTime(balance['updated-at']) })
}

export function compactPosition(position: JsonObject): Row {
  return compactRow(
    {
      symbol: jsonText(position.symbol),
      type: jsonText(position['instrument-type']),
      underlying: jsonText(position['underlying-symbol']),
      qty: num(position.quantity),
      dir: jsonText(position['quantity-direction']),
      avg_open: num(position['average-open-price']),
      mark: num(position['mark-price'] ?? position.mark),
      day_gain: num(signedField(position, 'realized-day-gain')),
      today: num(signedField(position, 'realized-today')),
      expires: jsonTime(position['expires-at']),
    },
    { dropZero: true },
  )
}

const MAX_DESCRIPTION_CHARS = 80

function compactText(value: JsonValue | undefined): string | undefined {
  const text = jsonText(value)
  if (!text) return undefined
  return text.length <= MAX_DESCRIPTION_CHARS ? text : `${text.slice(0, MAX_DESCRIPTION_CHARS - 3)}...`
}

function compactOrderLegs(legs: JsonValue | undefined): string {
  if (!Array.isArray(legs) || !legs.length) throw new Error('Broker order is missing legs')
  return legs
    .map((value) => {
      const leg = jsonObject(value)
      const action = jsonText(leg?.action)
      const quantity = num(leg?.quantity)
      const symbol = jsonText(leg?.symbol)
      if (!action || !quantity || !symbol) throw new Error('Broker order leg is missing action, quantity, or symbol')
      return `${action} ${quantity} ${symbol}`
    })
    .join('; ')
}

export function compactOrder(order: JsonObject): Row {
  return compactRow(
    {
      id: jsonId(order.id),
      status: jsonText(order.status),
      underlying: jsonText(order['underlying-symbol']),
      type: jsonText(order['order-type']),
      tif: jsonText(order['time-in-force']),
      price: num(signedField(order, 'price')),
      size: num(order.size),
      legs: compactOrderLegs(order.legs),
      received_at: jsonTime(order['received-at']),
      updated_at: jsonTime(order['updated-at']),
      reject_reason: jsonText(order['reject-reason']),
    },
    { dropZero: true },
  )
}

/** The per-trade fee components Tastytrade reports on transactions and order previews. */
export const FEE_FIELDS = ['regulatory-fees', 'clearing-fees', 'commission', 'proprietary-index-option-fees'] as const

function compactTransaction(transaction: JsonObject): Row {
  const fees = [...FEE_FIELDS, 'other-charge'].reduce((total, field) => {
    const value = signedField(transaction, field)
    return value === undefined ? total : total.plus(value)
  }, new Decimal(0))
  return compactRow(
    {
      date: jsonTime(transaction['executed-at']) ?? jsonText(transaction['transaction-date']),
      type: jsonText(transaction['transaction-type']),
      sub_type: jsonText(transaction['transaction-sub-type']),
      symbol: jsonText(transaction.symbol),
      action: jsonText(transaction.action),
      qty: num(transaction.quantity),
      price: num(transaction.price),
      value: num(signedField(transaction, 'value')),
      net: num(signedField(transaction, 'net-value')),
      fees: fees.isZero() ? undefined : num(fees),
      order_id: jsonId(transaction['order-id']),
      desc: compactText(transaction.description),
    },
    { dropZero: true },
  )
}

export async function accountOverview(broker: Broker, { include }: z.infer<typeof OverviewInput>): Promise<Row> {
  const account = await broker.accountPath()
  const [balances, positions] = await Promise.all([
    // Balances come back one row per currency; the USD row is the account's.
    include.includes('balances')
      ? requestItems(broker.client, `${account}/balances`).then((rows) => rows.find((row) => row.currency === 'USD') ?? rows[0])
      : undefined,
    include.includes('positions')
      ? requestItems(broker.client, `${account}/positions`, { query: { 'include-marks': true } })
      : undefined,
  ])
  return compactRow({ balances: balances && compactBalances(balances), positions: positions?.map(compactPosition) })
}

export async function accountHistory(broker: Broker, query: z.infer<typeof HistoryInput>, now = new Date()): Promise<string> {
  const days = query.days ?? (query.type === 'transactions' ? 90 : 7)
  const account = await broker.accountPath()
  const common = {
    'per-page': query.limit,
    'page-offset': query.page_offset,
    'start-date': nyDate(new Date(now.getTime() - days * 24 * 60 * 60 * 1000)),
    'underlying-symbol': query.underlying_symbol,
  }
  if (query.type === 'transactions') {
    const rows = await requestItems(broker.client, `${account}/transactions`, {
      query: { ...common, sort: 'Desc', type: query.transaction_type },
    })
    return toTable(rows.map(compactTransaction))
  }
  return toTable((await requestItems(broker.client, `${account}/orders`, { query: common })).map(compactOrder))
}
