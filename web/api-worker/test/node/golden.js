// Reading the Python's saved results (tests/golden/, written by scripts/make_golden.py).
import { readdirSync, readFileSync } from 'node:fs';
import { gunzipSync } from 'node:zlib';
import { fileURLToPath } from 'node:url';

export const GOLDEN = fileURLToPath(new URL('../../../../tests/golden/', import.meta.url));

export const load = (path) => {
  const raw = readFileSync(GOLDEN + path);
  return JSON.parse(path.endsWith('.gz') ? gunzipSync(raw).toString() : raw.toString());
};

export const list = (dir) => {
  try {
    return readdirSync(GOLDEN + dir).filter((f) => f.endsWith('.json.gz')).sort();
  } catch {
    return [];
  }
};

/** |a - b| within rel x max(|a|, |b|) or abs. */
export const close = (a, b, rel = 0.01, abs = 1e-9) => Math.abs(a - b) <= Math.max(abs, rel * Math.max(Math.abs(a), Math.abs(b)));
