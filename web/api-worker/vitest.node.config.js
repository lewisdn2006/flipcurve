import { defineConfig } from 'vitest/config';

// Pure calculation (engine, page readers) checked against the Python's saved results in
// tests/golden/, in Node so the tests can read those files. Routes run in the Workers runtime
// (vitest.config.js).
export default defineConfig({ test: { include: ['test/node/**/*.test.js'], testTimeout: 60000 } });
