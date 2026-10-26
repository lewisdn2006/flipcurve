// Page reading against the Python: each saved real scan (tests/fixtures/pages, on the laptop only:
// signed-in pages stay out of git) through ingest from empty storage, as scripts/make_golden.py did.
import { existsSync, readFileSync } from 'node:fs';
import { gunzipSync } from 'node:zlib';
import { describe, expect, it } from 'vitest';
import { ingest } from '../../src/scrapers/ingest.js';
import { MemoryStore } from '../../src/store.js';
import { differences } from './compare.js';
import { GOLDEN, list, load } from './golden.js';

const CAPTURES = GOLDEN + '../fixtures/pages/';
const cases = list('pages').filter((f) => existsSync(CAPTURES + f));

describe.skipIf(!cases.length)('real scans', () => {
  it.each(cases)('%s gives the same rows and stats', async (f) => {
    const want = load('pages/' + f);
    const req = JSON.parse(gunzipSync(readFileSync(CAPTURES + f)).toString());
    const [rows, stats] = await ingest(req.pages, req.item.search_queries, { store: new MemoryStore(), env: {} });
    expect(differences(stats, want.stats, { rel: 0, abs: 1e-9 })).toEqual([]);
    expect(rows.length).toBe(want.rows.length);
    expect(differences(rows, want.rows, { rel: 0, abs: 2e-4 }).slice(0, 20)).toEqual([]);
  });
});
