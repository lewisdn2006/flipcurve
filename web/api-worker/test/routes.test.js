import { describe, expect, it } from 'vitest';
import { env, exports } from 'cloudflare:workers';

const call = (path, init) => exports.default.fetch(new Request('https://api.test' + path, init));

describe('health', () => {
  it('answers like the Python server, plus the D1 counts', async () => {
    await env.DB.prepare("INSERT INTO ebay_calibration_points VALUES (336744200000, '336', 1792900000, 'search')").run();
    const res = await call('/api/v1/health');
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body).toMatchObject({ ok: true, ebay_api: false, runtime: 'worker' });
    expect(body.data).toEqual({ ebay_calibration_points: 1, vinted_calibration: 0, saved_scans: 0, vinted_seen: 0 });
  });
});

describe('CORS', () => {
  it('lets lewisdn.com and the extension in', async () => {
    for (const origin of ['https://lewisdn.com', 'https://www.lewisdn.com', 'http://localhost:8000',
      'chrome-extension://fdibajhmjjbjcdfafdofkoedbkhbkfcb']) {
      const res = await call('/api/v1/health', { method: 'OPTIONS', headers: { Origin: origin } });
      expect(res.status).toBe(204);
      expect(res.headers.get('Access-Control-Allow-Origin')).toBe(origin);
      expect(res.headers.get('Access-Control-Allow-Headers')).toContain('Content-Encoding');
    }
  });

  it('gives other sites nothing', async () => {
    for (const origin of ['https://evil.com', 'https://lewisdn.com.evil.com', 'chrome-extension://someoneelse']) {
      const res = await call('/api/v1/health', { headers: { Origin: origin } });
      expect(res.headers.get('Access-Control-Allow-Origin')).toBeNull();
    }
  });
});

describe('everything else', () => {
  it('404s unknown routes, in FastAPI wording', async () => {
    const res = await call('/api/v1/nothing-here', { method: 'POST', body: '{}' });
    expect(res.status).toBe(404);
    expect(await res.json()).toEqual({ detail: 'Not Found' });
  });

  it('sends / to the FlipCurve page', async () => {
    const res = await call('/', { redirect: 'manual', headers: { Origin: 'https://lewisdn.com' } });
    expect(res.status).toBe(302);
    expect(res.headers.get('Location')).toBe('https://lewisdn.com/flipcurve/');
    expect(res.headers.get('Access-Control-Allow-Origin')).toBe('https://lewisdn.com');
  });
});
