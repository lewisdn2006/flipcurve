// The cheap status check must give the same answer as the full page reader on real listing pages
// (tests/fixtures/pages, on the laptop) and on the scan tests' made-up ones.
import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { gunzipSync } from 'node:zlib';
import { describe, expect, it } from 'vitest';
import { classifyItemPage } from '../../src/scrapers/classify.js';
import { parseItemPage } from '../../src/scrapers/vinted.js';
import { GOLDEN, load } from './golden.js';

const DIR = GOLDEN + '../fixtures/pages/';
const pages = [];
if (existsSync(DIR)) {
  for (const f of readdirSync(DIR).filter((x) => x.endsWith('.json.gz'))) {
    const req = JSON.parse(gunzipSync(readFileSync(DIR + f)).toString());
    for (const p of req.pages || (req.results || []).map((r) => r.page)) {
      if (p.platform === 'vinted' && p.kind !== 'catalog') pages.push([`${f} ${p.url.split('/').at(-1)}`, p]);
    }
  }
}
const history = load('scenarios/vinted-history.json.gz');
for (const p of [...history.scan2, ...history.background.map((b) => b.page)]) {
  if (p.kind !== 'catalog' && p.platform === 'vinted') pages.push([`made-up ${p.kind} ${p.url.split('/').at(-1)}`, p]);
}

describe('classifyItemPage agrees with parseItemPage', () => {
  it.each(pages)('%s', (_, p) => {
    expect(classifyItemPage(p.html, p.status)).toBe(parseItemPage(p.html, p.status).status);
  });
  it('handles the edge cases', () => {
    for (const [html, status] of [
      [null, 404], ['<div>Sold</div>', 404],
      ['<script type="application/ld+json">{"offers":{"availability":"InStock"}}</script><button data-testid="item-buy-button">Buy</button>', 200],
      ['<script type="application/ld+json">{"offers":{"availability":"InStock"}}</script><div>Sold</div>', 200],
      ['<script type="application/ld+json">[1,2]</script><div>Sold out</div>', 200],
      ['<div>Unsold</div><span>SOLD</span>', 200],
      ['<script>var x = "Sold";</script><p>for sale</p>', 200],
      ['<title>Session refresh</title>', 200],
    ]) expect(classifyItemPage(html, status), html).toBe(parseItemPage(html, status).status);
  });
});
