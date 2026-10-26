import { describe, expect, it } from 'vitest';
import { digamma, gammainc, gammaln, invert, median, minimizeBounded, npRound, percentile, pinvSym, pyRound } from '../../src/engine/numeric.js';
import { load } from './golden.js';

const maths = load('maths.json');

describe('special functions match scipy', () => {
  it('gammaln', () => {
    for (const [x, want] of maths.gammaln) expect(Math.abs(gammaln(x) - want)).toBeLessThan(1e-12 * Math.max(1, Math.abs(want)));
  });
  it('digamma', () => {
    for (const [x, want] of maths.digamma) expect(Math.abs(digamma(x) - want)).toBeLessThan(1e-12 * Math.max(1, Math.abs(want)));
  });
  it('gammainc', () => {
    let worst = 0;
    for (const [a, x, want] of maths.gammainc) {
      const got = gammainc(a, x);
      // the model takes log(P + 1e-300), so relative accuracy matters for small P
      const err = want > 1e-250 ? Math.abs(got - want) / want : Math.abs(got - want);
      worst = Math.max(worst, err);
    }
    expect(worst).toBeLessThan(1e-10);
  });
});

describe('statistics like numpy', () => {
  it('median and percentile', () => {
    expect(median([3, 1, 2])).toBe(2);
    expect(median([4, 1, 2, 3])).toBe(2.5);
    expect(percentile([1, 2, 3, 4], 25)).toBe(1.75);
    expect(percentile([10, 20, 30, 40, 50], 90)).toBe(46);
  });
});

describe('bounded minimiser', () => {
  it('finds a minimum inside the box', () => {
    const f = ([a, b]) => (a - 1) ** 2 + 10 * (b + 2) ** 2 + a * b;
    const r = minimizeBounded(f, [0, 0], [[null, null], [null, null]]);
    // 2(a-1) + b = 0 and 20(b+2) + a = 0 -> a = 80/39, b = 2 - 2a
    expect(r.x[0]).toBeCloseTo(80 / 39, 6);
    expect(r.x[1]).toBeCloseTo(2 - 160 / 39, 6);
  });
  it('stops at a bound', () => {
    const r = minimizeBounded(([a, b]) => (a - 5) ** 2 + (b - 1) ** 2, [0, 0], [[null, 2], [0.5, null]]);
    expect(r.x[0]).toBeCloseTo(2, 8);
    expect(r.x[1]).toBeCloseTo(1, 5);
  });
  it('handles a curved valley (Rosenbrock)', () => {
    const r = minimizeBounded(([a, b]) => (1 - a) ** 2 + 100 * (b - a * a) ** 2, [-1.2, 1], [[null, null], [null, null]]);
    expect(r.x[0]).toBeCloseTo(1, 4);
    expect(r.x[1]).toBeCloseTo(1, 4);
  });
});

describe('matrices', () => {
  it('invert and pseudo-invert', () => {
    const A = [[4, 1], [1, 3]];
    const inv = invert(A);
    expect(inv[0][0]).toBeCloseTo(3 / 11, 12);
    expect(inv[0][1]).toBeCloseTo(-1 / 11, 12);
    expect(invert([[1, 2], [2, 4]])).toBeNull();
    const p = pinvSym([[1, 2], [2, 4]]);  // rank 1: pinv = A / 25
    expect(p[0][0]).toBeCloseTo(1 / 25, 12);
    expect(p[1][1]).toBeCloseTo(4 / 25, 12);
  });
});

describe('rounding like Python and numpy', () => {
  it('round() uses the exact stored value, ties to even', () => {
    expect(pyRound(47.775, 2)).toBe(47.77);   // stored as 47.77499...
    expect(pyRound(2.675, 2)).toBe(2.67);
    expect(pyRound(0.125, 2)).toBe(0.12);     // an exact tie: to even
    expect(pyRound(0.375, 2)).toBe(0.38);
    expect(pyRound(2.5)).toBe(2);
    expect(pyRound(-1.005, 2)).toBe(-1);      // stored as -1.00499...
    expect(pyRound(12.3456, 1)).toBe(12.3);
  });
  it('np.round scales first', () => {
    expect(npRound(2.5)).toBe(2);
    expect(npRound(3.5)).toBe(4);
  });
});
