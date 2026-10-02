import { Decimal } from 'decimal.js'

import { jsonDecimal, signedField, type JsonObject } from './tastytrade/json.js'

type Cell = string | number | boolean | null | undefined | Cell[] | { [key: string]: Cell }
export type Row = Record<string, Cell>

const NUMERIC = /^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$/

/** A decimal without trailing zeros or exponent: "1234.50" → "1234.5", "3.000" → "3". */
export function decimalText(value: Decimal): string {
  if (!value.isFinite()) throw new Error(`Cannot compact non-finite value: ${value.toString()}`)
  return value.toFixed()
}

/** A broker numeric field as compact text; non-numeric text is returned unchanged. */
export function num(value: unknown): string | undefined {
  const decimal = value instanceof Decimal ? value : jsonDecimal(value)
  if (decimal?.isFinite()) return decimalText(decimal)
  return typeof value === 'string' && value ? value : undefined
}

function isEmptyCell(value: Cell, dropZero = false): boolean {
  if (value === undefined || value === null || value === '') return true
  if (Array.isArray(value)) return value.length === 0
  if (typeof value === 'object') return Object.keys(value).length === 0
  return dropZero && value === '0'
}

/** Drops absent fields (and the "0" string when asked) so a row carries only what it says. */
export function compactRow<T extends Row>(row: T, options: { dropZero?: boolean } = {}): Partial<T> {
  return Object.fromEntries(Object.entries(row).filter(([, value]) => !isEmptyCell(value, options.dropZero))) as Partial<T>
}

/** Signed money fields renamed per `fields` (source → output), zeros and absences dropped. */
export function signedMoneyRow(row: JsonObject, fields: Record<string, string>): Row {
  const money: Row = {}
  for (const [source, output] of Object.entries(fields)) money[output] = num(signedField(row, source))
  return compactRow(money, { dropZero: true })
}

export function chunks<T>(items: readonly T[], size: number): T[][] {
  const result: T[][] = []
  for (let start = 0; start < items.length; start += size) result.push(items.slice(start, start + size))
  return result
}

/** A plain aligned table: headers, then one line per row; numeric columns right-aligned. */
export function toTable(rows: readonly Row[]): string {
  if (!rows.length) return 'No data'
  const headers: string[] = []
  for (const row of rows) {
    for (const key of Object.keys(row)) if (!headers.includes(key)) headers.push(key)
  }
  const cells = rows.map((row) => headers.map((header) => cellText(row[header])))
  const numeric = headers.map((_, column) => cells.every((line) => line[column] === '' || NUMERIC.test(line[column]!)))
  const widths = headers.map((header, column) => Math.max(header.length, ...cells.map((line) => line[column]!.length)))
  const format = (line: string[]) =>
    line
      .map((cell, column) => (numeric[column] ? cell.padStart(widths[column]!) : cell.padEnd(widths[column]!)))
      .join('  ')
      .trimEnd()
  return [format(headers), ...cells.map(format)].join('\n')
}

function cellText(value: Cell): string {
  if (value === undefined || value === null) return ''
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

/** Wraps tool output in a named element; the body is compact JSON or a table, XML-escaped. */
export function toolXml(tag: string, payload: unknown): string {
  const text = typeof payload === 'string' ? payload : JSON.stringify(payload)
  if (text === undefined) throw new Error(`Cannot serialize ${tag} output`)
  return `<${tag}>${text.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')}</${tag}>`
}
