// The Vinted history (re-checks across scans, background re-checks, what is due when) replayed step by
// step against the Python's recording (scripts/make_golden.py, made-up pages from test_scan_pages.py).
import { describe, expect, it } from 'vitest';
import { applyBackgroundRechecks, dueRechecks, ingest, markRelevant, recheckIds, slug } from '../../src/scrapers/ingest.js';
import { MemoryStore } from '../../src/store.js';
import { differences } from './compare.js';
import { load } from './golden.js';

const g = load('scenarios/vinted-history.json.gz');
const t0 = Date.parse(g.t0);
const H = 3600000;

describe('Vinted history, step by step', () => {
  const store = new MemoryStore();
  const q = g.item.search_queries;
  const steps = g.steps.slice();
  const next = (name) => {
    const s = steps.shift();
    expect(s.step).toBe(name);
    return s;
  };

  it('replays every step the same way', async () => {
    let s = next('scan1');
    let [rows, stats] = await ingest(g.scan1, q, { store, env: {} });
    expect(differences(stats, s.stats, { rel: 0, abs: 1e-9 })).toEqual([]);
    expect(differences(rows, s.rows, { rel: 0, abs: 2e-4 })).toEqual([]);

    s = next('mark_relevant');
    await markRelevant(store, q.vinted, s.ids);

    for (const h of [1, 4]) {
      s = next('due');
      expect(s.hours).toBe(h);
      expect(await dueRechecks(store, 20, t0 + h * H)).toEqual(s.out);
    }
    s = next('recheck_ids');
    expect(await recheckIds(store, q.vinted, t0 + 8 * H)).toEqual(s.out);

    s = next('scan2');
    [rows, stats] = await ingest(g.scan2, q, { store, env: {} });
    expect(differences(stats, s.stats, { rel: 0, abs: 1e-9 })).toEqual([]);
    expect(differences(rows, s.rows, { rel: 0, abs: 2e-4 })).toEqual([]);

    s = next('background');
    expect(await applyBackgroundRechecks(g.background, { store })).toEqual(s.out);
    const saved = await store.loadCache(slug(q.vinted), 'vinted');
    expect(differences(saved.rows, s.saved, { rel: 0, abs: 2e-4 })).toEqual([]);

    for (const h of [16, 23, 30]) {
      s = next('due');
      expect(await dueRechecks(store, 20, t0 + h * H), `due at ${h} h`).toEqual(s.out);
    }
    expect(steps).toEqual([]);
  });
});
