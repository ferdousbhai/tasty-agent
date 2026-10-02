import { Decimal } from 'decimal.js'

export type JsonValue = string | number | boolean | null | JsonValue[] | { [key: string]: JsonValue }
export type JsonObject = { [key: string]: JsonValue }

export function isJsonObject(value: unknown): value is JsonObject {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

export function jsonObject(value: unknown): JsonObject | undefined {
  return isJsonObject(value) ? value : undefined
}

/** The objects in an array; anything that is not an array of objects contributes nothing. */
export function jsonObjects(value: unknown): JsonObject[] {
  return Array.isArray(value) ? value.filter(isJsonObject) : []
}

/** A non-empty string, or a finite number rendered as one; anything else is absent. */
export function jsonText(value: unknown): string | undefined {
  if (typeof value === 'string') return value.length ? value : undefined
  if (typeof value === 'number' && Number.isFinite(value)) return String(value)
  return undefined
}

/** A timestamp field as ISO text; Tastytrade sends some as epoch milliseconds, and 0 for "none". */
export function jsonTime(value: unknown): string | undefined {
  if (typeof value !== 'number') return jsonText(value)
  return Number.isFinite(value) && value > 0 ? new Date(value).toISOString() : undefined
}

/** An identifier field: numbers stay numbers, anything else is read as text. */
export function jsonId(value: unknown): number | string | undefined {
  return typeof value === 'number' ? value : jsonText(value)
}

/** A finite decimal from a string or number field; anything else, including "NaN", is absent. */
export function jsonDecimal(value: unknown): Decimal | undefined {
  const text = jsonText(value)?.trim()
  if (!text) return undefined
  try {
    const decimal = new Decimal(text)
    return decimal.isFinite() ? decimal : undefined
  } catch {
    return undefined
  }
}

/** The `items` collection Tastytrade wraps list responses in. */
export function jsonItems(data: JsonValue, label: string): JsonObject[] {
  const items = jsonObject(data)?.items
  if (!Array.isArray(items)) throw new Error(`${label}: response is missing items`)
  return items.map((item, index) => {
    if (!isJsonObject(item)) throw new Error(`${label}: item ${index} is not an object`)
    return item
  })
}

/** Tastytrade sends money unsigned beside a `<field>-effect` of Debit or Credit. */
export function signedField(row: JsonObject, field: string): string | undefined {
  const value = jsonText(row[field])
  if (value === undefined) return undefined
  if (row[`${field}-effect`] !== 'Debit') return value
  return value.startsWith('-') ? value : `-${value}`
}

/** A broker `{code, message}` entry as "code: message" (either half alone when the other is absent). */
export function brokerMessageText(entry: JsonObject): string | undefined {
  const code = jsonText(entry.code)
  const message = jsonText(entry.message)
  return code && message ? `${code}: ${message}` : (code ?? message)
}
