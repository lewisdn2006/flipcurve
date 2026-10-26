// Deep comparison of a JS result with the Python's, listing every difference beyond tolerance.

/** Numbers match when within `rel` of each other or within `abs` (one unit in the last rounded place). */
export function differences(got, want, { rel = 0.01, abs = 0.0101, skip = [] } = {}, path = '', out = []) {
  if (skip.some((s) => path === s || path.startsWith(s + '.'))) return out;
  if (typeof want === 'number' && typeof got === 'number') {
    // days are shown to one decimal: the optimisers' last digits can tip one the other way
    const a = /days/.test(path.split('.').at(-1)) ? Math.max(abs, 0.1001) : abs;
    if (Math.abs(got - want) > Math.max(a, rel * Math.max(Math.abs(got), Math.abs(want)))) out.push(`${path}: ${got} vs ${want}`);
  } else if (Array.isArray(want)) {
    if (!Array.isArray(got) || got.length !== want.length) out.push(`${path}: length ${got?.length} vs ${want.length}`);
    else want.forEach((w, i) => differences(got[i], w, { rel, abs, skip }, `${path}[${i}]`, out));
  } else if (want !== null && typeof want === 'object') {
    if (got === null || typeof got !== 'object') { out.push(`${path}: ${JSON.stringify(got)} vs object`); return out; }
    for (const k of new Set([...Object.keys(want), ...Object.keys(got)])) {
      differences(got[k], want[k], { rel, abs, skip }, path ? `${path}.${k}` : k, out);
    }
  } else if (got !== want && !(got == null && want == null)) {
    out.push(`${path}: ${JSON.stringify(got)} vs ${JSON.stringify(want)}`);
  }
  return out;
}
