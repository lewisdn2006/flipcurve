// A Vinted listing page's status without parsing the HTML: 'active', 'sold', 'deleted' or 'unknown',
// the same answer as parseItemPage (vinted.js, after vinted_parse.py) gives. Cheap enough for the
// Worker's free-plan CPU budget, and copied into the helper extension (extension/background.js,
// classifyItemPage), which sends only the status of its background re-checks. Keep the two in step.

const LD_JSON = /<script\b[^>]*\btype\s*=\s*["']?application\/ld\+json["']?[^>]*>([\s\S]*?)<\/script\s*>/i;
const BUY_BUTTON = /\bdata-testid\s*=\s*["']item-buy-button["']/i;
const HIDDEN = /<(script|style|template)\b[^>]*>[\s\S]*?<\/\1\s*>/gi;

const decode = (s) => s.replace(/&(amp|lt|gt|quot|#39|apos);/g, (_, e) => ({ amp: '&', lt: '<', gt: '>', quot: '"', '#39': "'", apos: "'" }[e]));

export function classifyItemPage(html, httpStatus) {
  if (httpStatus === 404 || html == null) return 'deleted';
  let avail = null;
  const ld = html.match(LD_JSON);
  if (ld) {
    try {
      const j = JSON.parse(decode(ld[1]));
      if (j && typeof j === 'object' && !Array.isArray(j) && j.offers && typeof j.offers === 'object') avail = j.offers.availability ?? null;
    } catch { /* not JSON */ }
  }
  if (avail === 'InStock' && BUY_BUTTON.test(html)) return 'active';
  if (avail === null && /\bSold\b/.test(decode(html.replace(HIDDEN, ' ').replace(/<[^>]*>/g, ' ')))) return 'sold';
  return 'unknown';
}
