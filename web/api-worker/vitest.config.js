import { defineConfig } from 'vitest/config';
import { cloudflareTest, readD1Migrations } from '@cloudflare/vitest-plugin';

// Tests run inside the Workers runtime with a local, empty D1 that gets the migrations first.
export default defineConfig(async () => ({
  plugins: [
    cloudflareTest({
      wrangler: { configPath: './wrangler.jsonc' },
      miniflare: { bindings: { TEST_MIGRATIONS: await readD1Migrations('./migrations') } },
    }),
  ],
  test: { setupFiles: ['./test/apply-migrations.js'], exclude: ['test/node/**', 'node_modules/**'] },
}));
