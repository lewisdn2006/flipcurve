// HTML parsing for the page readers: linkedom, plus BeautifulSoup's text rules (script and style
// contents are not text; strings are stripped, empty ones dropped).
import { parseHTML } from 'linkedom';

export const parse = (html) => parseHTML(/<html[\s>]/i.test(html || '') ? html : `<!doctype html><html><body>${html || ''}</body></html>`).document;

const SKIP = new Set(['SCRIPT', 'STYLE', 'TEMPLATE']);

/** Every text piece under a node, like BeautifulSoup's .strings (unstripped). linkedom splits text
 *  around entities ("B&amp;T" is three nodes), so neighbouring text nodes are joined first, as
 *  BeautifulSoup does. */
export function strings(node) {
  const out = [];
  const walk = (n) => {
    let prevText = false;
    for (const c of n.childNodes) {
      if (c.nodeType === 3) {
        if (prevText) out[out.length - 1] += c.nodeValue;
        else out.push(c.nodeValue);
        prevText = true;
        continue;
      }
      prevText = false;
      if (c.nodeType === 1 && !SKIP.has(c.tagName)) walk(c);
    }
  };
  walk(node);
  return out;
}

/** BeautifulSoup's .stripped_strings. */
export const strippedStrings = (node) => strings(node).map((s) => s.trim()).filter(Boolean);

/** BeautifulSoup's get_text(sep, strip=...). */
export const getText = (node, sep = '', strip = false) => (strip ? strippedStrings(node) : strings(node)).join(sep);

/** An attribute by name, ignoring case as HTML does (linkedom keeps names as written: itemProp). */
export function attr(el, name) {
  const want = name.toLowerCase();
  for (const a of el.attributes || []) if (a.name.toLowerCase() === want) return a.value;
  return null;
}
