// Dates the way the Python code wrote them: UK wall-clock times (eBay shows those), Python's
// isoformat() strings, and datetime.fromtimestamp()'s rounding to microseconds.

import { pyRound } from './engine/numeric.js';

export const UK = 'Europe/London';
export const DAY_MS = 86400000;

const fmtCache = new Map();
/** Wall-clock parts of a moment (ms) in a time zone. */
export function zonedParts(ms, timeZone) {
  if (!fmtCache.has(timeZone)) {
    fmtCache.set(timeZone, new Intl.DateTimeFormat('en-GB', { timeZone, hourCycle: 'h23', year: 'numeric', month: '2-digit',
      day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' }));
  }
  const p = Object.fromEntries(fmtCache.get(timeZone).formatToParts(new Date(ms)).map((x) => [x.type, Number(x.value)]));
  return { y: p.year, mo: p.month, d: p.day, h: p.hour, mi: p.minute, s: p.second };
}

/** The zone's UTC offset at a moment, in ms. */
export function offsetAt(ms, timeZone) {
  if (timeZone === 'UTC') return 0;
  const p = zonedParts(ms, timeZone);
  return Date.UTC(p.y, p.mo - 1, p.d, p.h, p.mi, p.s) - Math.floor(ms / 1000) * 1000;
}

/** A wall-clock time in a zone -> ms. Ambiguous times (clocks going back) take the first, as Python's
 *  fold=0 does; days and months may overflow (d + 1 is the next day). */
export function zonedToUtc(y, mo, d, h = 0, mi = 0, s = 0, timeZone = UK) {
  const wall = Date.UTC(y, mo - 1, d, h, mi, s);
  const offsets = new Set([offsetAt(wall - DAY_MS, timeZone), offsetAt(wall + DAY_MS, timeZone)]);
  const valid = [...offsets].map((o) => wall - o).filter((t) => offsetAt(t, timeZone) === wall - t).sort((a, b) => a - b);
  return valid.length ? valid[0] : wall - offsetAt(wall - DAY_MS, timeZone);  // in a gap: the offset before it
}

const pad = (n, w = 2) => String(n).padStart(w, '0');

/** datetime.isoformat() of a moment (ms, plus optional extra microseconds) in a zone. */
export function isoformat(ms, timeZone = 'UTC', micro = null) {
  const whole = Math.floor(ms / 1000) * 1000;
  const us = micro ?? (ms - whole) * 1000;
  const off = offsetAt(whole, timeZone);
  const w = new Date(whole + off);
  const sign = off < 0 ? '-' : '+';
  const oa = Math.abs(off) / 60000;
  return `${w.getUTCFullYear()}-${pad(w.getUTCMonth() + 1)}-${pad(w.getUTCDate())}T${pad(w.getUTCHours())}:`
    + `${pad(w.getUTCMinutes())}:${pad(w.getUTCSeconds())}${us ? '.' + pad(Math.round(us), 6) : ''}`
    + `${sign}${pad(Math.floor(oa / 60))}:${pad(oa % 60)}`;
}

/** datetime.fromtimestamp(ts, timezone.utc).isoformat(): microseconds rounded half to even. */
export function isoFromTs(ts) {
  let whole = Math.floor(ts);
  let us = pyRound((ts - whole) * 1e6);
  if (us >= 1e6) { whole += 1; us -= 1e6; }
  return isoformat(whole * 1000, 'UTC', us);
}

/** ISO text (with or without a zone; none = UTC, as the Python treats it) -> ms, or null. */
export function parseTime(v) {
  if (!v) return null;
  let s = String(v).trim().replace(' ', 'T').replace(/(\.\d{3})\d+/, '$1');
  if (/T\d/.test(s) && !/(Z|[+-]\d\d:?\d\d)$/.test(s)) s += 'Z';
  const t = Date.parse(s);
  return Number.isNaN(t) ? null : t;
}
