import { readFileSync } from 'node:fs';
import { gunzipSync } from 'node:zlib';
import { defineConfig } from 'vitest/config';
import { cloudflareTest, readD1Migrations } from '@cloudflare/vitest-plugin';

// Tests run inside the Workers runtime with a local, empty D1 that gets the migrations first. The
// runtime can't read files, so the Python's recorded Vinted history scenario is handed in here.
const golden = (p) => JSON.parse(gunzipSync(readFileSync(new URL(`../../tests/golden/${p}`, import.meta.url))).toString());

export default defineConfig(async () => ({
  plugins: [
    cloudflareTest({
      wrangler: { configPath: './wrangler.jsonc' },
      miniflare: { bindings: { TEST_MIGRATIONS: await readD1Migrations('./migrations') } },
    }),
  ],
  test: {
    setupFiles: ['./test/apply-migrations.js'],
    exclude: ['test/node/**', 'node_modules/**'],
    provide: { history: golden('scenarios/vinted-history.json.gz') },
  },
}));
