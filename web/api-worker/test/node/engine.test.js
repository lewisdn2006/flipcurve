// The engine against the Python's saved results (scripts/make_golden.py): six saved items, and every
// real scan that has a response.
import { describe, expect, it } from 'vitest';
import { run } from '../../src/engine/pipeline.js';
import { differences } from './compare.js';
import { list, load } from './golden.js';

const cases = [
  ...list('engine').map((f) => ['engine/' + f, load('engine/' + f)]),
  ...list('pages').map((f) => ['pages/' + f, load('pages/' + f)]).filter(([, g]) => g.response),
];

describe.each(cases)('%s', (name, g) => {
  let fit;
  const out = run(g.response.item_metadata, g.rows, { now: g.now, dataSource: g.response.market_summary.data_source,
    onFit: (f) => { fit = f; } });

  it('fits the same model', () => {
    const want = g.fits[0];
    expect(fit.n_obs).toBe(want.n_obs);
    expect(fit.n_sales).toEqual(want.n_sales);
    expect(fit.anchors).toEqual(want.anchors);
    expect(fit.sigma_p).toBeCloseTo(want.sigma_p, 9);
    // the optimum, not where each optimiser happened to stop: scipy stops at a looser tolerance
    fit.psi.forEach((p, i) => expect(Math.abs(p - want.psi[i]), `psi[${i}]`).toBeLessThan(5e-3));
    expect(Math.abs(fit.log_likelihood - want.log_likelihood)).toBeLessThan(1e-3 * Math.max(1, Math.abs(want.log_likelihood)));
  });

  it('gives the same response', () => {
    const diffs = differences(out, g.response, { skip: ['comparable_listings'] });
    expect(diffs.slice(0, 30)).toEqual([]);
  });

  it('shows the same comparable listings (or the same number, when sampled)', () => {
    const want = g.response.comparable_listings;
    expect(out.comparable_listings.length).toBe(want.length);
    if (want.length < 500) expect(out.comparable_listings.map((r) => r.listing_id)).toEqual(want.map((r) => r.listing_id));
  });
});
