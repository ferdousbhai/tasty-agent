import { Decimal } from 'decimal.js'
import { z } from 'zod'

import { compactOrder, FEE_FIELDS as TRANSACTION_FEE_FIELDS } from './account.js'
import type { Broker } from './broker.js'
import { compactRow, decimalText, signedMoneyRow, toTable, type Row } from './compact.js'
import { OrderLegSchema, resolveInstrumentKind, type ResolvedLeg } from './instruments.js'
import { fetchQuoteRows, legQuote } from './market.js'
import { buildOrderMarket, formatOrderMarket, quantityGcd, resolveOrderPrice, sizeOrder } from './pricing.js'
import { requestItems } from './tastytrade/client.js'
import { errorMessage, TastytradeOutcomeUnknownError } from './tastytrade/errors.js'
import { brokerMessageText, jsonObject, jsonObjects, jsonText, type JsonObject, type JsonValue } from './tastytrade/json.js'

export const PlaceOrderInput = z
  .object({
  legs: z.array(OrderLegSchema).min(1).describe('Equities/options use Buy/Sell to Open/Close; futures use Buy/Sell.'),
  target_value: z.number().positive().optional().describe('Dollar budget; derives whole shares/contracts from current mid pricing.'),
  time_in_force: z
    .enum(['Day', 'GTC', 'Ext', 'Ext Overnight', 'GTC Ext', 'GTC Ext Overnight', 'IOC'])
    .default('Day')
    .describe('Default Day.'),
  dry_run: z.boolean().default(false).describe('Preview without sending.'),
})
  .superRefine((input, ctx) => {
    if (input.target_value === undefined) return
    if (quantityGcd(input.legs.map((leg) => leg.quantity)) !== 1) {
      ctx.addIssue({
        code: 'custom',
        message:
          'When sizing is supplied, leg quantities must be the smallest whole-number ratio ' +
          '(for a single option/stock use quantity=1).',
      })
    }
    if (input.legs.some((leg) => !['Equity', 'Equity Option'].includes(resolveInstrumentKind(leg)))) {
      ctx.addIssue({ code: 'custom', message: 'target_value sizing is currently supported for equities and equity options only' })
    }
  })
export const OrderIdInput = z.object({ order_id: z.string().min(1) })

/** A tick-aligned mid limit for the legs, with the market summary the caller sees. */
async function priceOrder(broker: Broker, legs: readonly ResolvedLeg[]) {
  try {
    const rows = await fetchQuoteRows(broker, legs.map((leg) => leg.detail))
    const market = buildOrderMarket(legs.map((leg) => ({ ...leg, quote: legQuote(rows.get(leg.detail.symbol)!, leg.detail.label) })))
    const { price, warnings } = resolveOrderPrice(market)
    if (price.isZero()) throw new Error('the resolved limit price is zero, which has no price effect')
    return {
      price,
      // Tastytrade takes an unsigned limit beside its price effect; the signed price is ours.
      fields: { price: decimalText(price.abs()), 'price-effect': price.isNegative() ? 'Debit' : 'Credit' },
      summary: compactRow({ limit: decimalText(price), market: formatOrderMarket(market), warnings }),
    }
  } catch (error) {
    throw new Error(`Could not resolve a safe limit price from live quotes: ${errorMessage(error)}`, { cause: error })
  }
}

const snakeCase = (fields: readonly string[]) => Object.fromEntries(fields.map((field) => [field, field.replaceAll('-', '_')]))
const BUYING_POWER_FIELDS = snakeCase([
  'change-in-margin-requirement',
  'change-in-buying-power',
  'current-buying-power',
  'new-buying-power',
  'isolated-order-margin-requirement',
  'impact',
])
const FEE_FIELDS = snakeCase([...TRANSACTION_FEE_FIELDS, 'total-fees'])

function brokerMessages(value: JsonValue | undefined): string[] {
  return jsonObjects(value).map((entry) => brokerMessageText(entry) ?? 'unspecified broker message')
}

class BrokerRejectedOrderError extends Error {
  override readonly name = 'BrokerRejectedOrderError'
}

/**
 * Reads an order envelope (`{order, buying-power-effect, fee-calculation, warnings, errors}`).
 * Broker errors are a refusal; a missing order or buying-power effect means the response cannot
 * be trusted to describe what would happen.
 */
function readOrderEnvelope(data: JsonValue): { result: Row; warnings: string[] } {
  const body = jsonObject(data) ?? {}
  const errors = brokerMessages(body.errors)
  if (errors.length) throw new BrokerRejectedOrderError(`Broker rejected order: ${errors.join('; ')}`)
  const warnings = brokerMessages(body.warnings)
  const order = jsonObject(body.order)
  const buyingPower = jsonObject(body['buying-power-effect'])
  if (!order || !buyingPower) {
    const context = warnings.length ? ` Warnings: ${warnings.join('; ')}` : ''
    throw new Error(`Broker order response is missing required order or buying-power data.${context}`)
  }
  const fees = jsonObject(body['fee-calculation'])
  return {
    result: compactRow({
      order: compactOrder(order),
      bp_effect: signedMoneyRow(buyingPower, BUYING_POWER_FIELDS),
      fees: fees && signedMoneyRow(fees, FEE_FIELDS),
      warnings,
    }),
    warnings,
  }
}

async function dryRun(broker: Broker, path: string, body: JsonObject) {
  return readOrderEnvelope(await broker.client.request(`${path}/dry-run`, { method: 'POST', body }))
}

function refuseOnWarnings(warnings: readonly string[]): void {
  if (warnings.length) {
    throw new Error(`Tastytrade returned dry-run warnings, so the order was not submitted: ${warnings.join('; ')}`)
  }
}

/** A 2xx after submission means the broker acted; an unreadable body must not read as a refusal. */
function acceptedOrder(read: () => Row): Row {
  try {
    return read()
  } catch (error) {
    if (error instanceof BrokerRejectedOrderError) throw error
    throw new TastytradeOutcomeUnknownError(
      `Tastytrade accepted the request but its response could not be read (${errorMessage(error)}). Check list_orders.`,
      { cause: error },
    )
  }
}

export async function placeOrder(broker: Broker, input: z.infer<typeof PlaceOrderInput>): Promise<Row> {
  const [details, account] = await Promise.all([broker.instruments.resolveAll(input.legs), broker.accountPath()])
  const index = details.find((detail) => detail.kind === 'Index')
  if (index) throw new Error(`Cannot place orders for index symbol '${index.symbol}' (quote-only)`)

  const legs = input.legs.map((leg, position) => ({ detail: details[position]!, action: leg.action, quantity: leg.quantity }))
  const priced = await priceOrder(broker, legs)
  const sizing = input.target_value === undefined ? undefined : sizeOrder(details, priced.price, new Decimal(input.target_value))
  const path = `${account}/orders`
  const body: JsonObject = {
    'time-in-force': input.time_in_force,
    'order-type': 'Limit',
    ...priced.fields,
    legs: legs.map(({ detail, action, quantity }) => ({
      'instrument-type': detail.kind,
      symbol: detail.symbol,
      quantity: quantity * (sizing?.quantity ?? 1),
      action,
    })),
  }
  const extras = compactRow({
    pricing: priced.summary,
    sizing: sizing && {
      target_value: decimalText(sizing.targetValue),
      unit_value: decimalText(sizing.unitValue),
      quantity: sizing.quantity,
      estimated_value: decimalText(sizing.estimatedValue),
    },
  })

  const preview = await dryRun(broker, path, body)
  if (input.dry_run) {
    const blocked = preview.warnings.length ? { blocked: 'Live placement would be refused: Tastytrade returned dry-run warnings.' } : {}
    return { ...preview.result, ...extras, ...blocked }
  }
  refuseOnWarnings(preview.warnings)
  const placed = await broker.client.request(path, { method: 'POST', body })
  return { ...acceptedOrder(() => readOrderEnvelope(placed).result), ...extras }
}

async function liveOrders(broker: Broker): Promise<JsonObject[]> {
  return requestItems(broker.client, `${await broker.accountPath()}/orders/live`)
}

export async function listOrders(broker: Broker): Promise<string> {
  return toTable((await liveOrders(broker)).map(compactOrder))
}

export async function replaceOrder(broker: Broker, { order_id: orderId }: z.infer<typeof OrderIdInput>): Promise<Row> {
  const order = (await liveOrders(broker)).find((candidate) => String(candidate.id) === orderId)
  if (!order) throw new Error(`Order ${orderId} not found in live orders`)
  const brokerLegs = jsonObjects(order.legs)
  const underlying = jsonText(order['underlying-symbol'])
  const timeInForce = jsonText(order['time-in-force'])
  if (!brokerLegs.length || !underlying || !timeInForce) {
    throw new Error(`Live order ${orderId} is missing legs, underlying-symbol, or time-in-force`)
  }

  const priced = await priceOrder(broker, await broker.instruments.resolveOrderLegs(brokerLegs, underlying))
  const gtcDate = jsonText(order['gtc-date'])
  const body: JsonObject = {
    'time-in-force': timeInForce,
    'order-type': 'Limit',
    ...priced.fields,
    ...(gtcDate ? { 'gtc-date': gtcDate } : {}),
  }

  const path = `${await broker.accountPath()}/orders/${encodeURIComponent(orderId)}`
  refuseOnWarnings((await dryRun(broker, path, body)).warnings)
  // A replacement answers with the new order itself, not the placement envelope.
  const replaced = jsonObject(await broker.client.request(path, { method: 'PUT', body }))
  return {
    order: acceptedOrder(() => compactOrder(jsonObject(replaced?.order) ?? replaced ?? {})),
    pricing: priced.summary,
  }
}

export async function cancelOrder(broker: Broker, { order_id: orderId }: z.infer<typeof OrderIdInput>): Promise<Row> {
  await broker.client.request(`${await broker.accountPath()}/orders/${encodeURIComponent(orderId)}`, { method: 'DELETE' })
  return { success: true, order_id: orderId }
}
