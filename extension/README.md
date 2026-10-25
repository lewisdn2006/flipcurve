# FlipCurve helper (Chrome extension)

A search-only helper with no screens. When you press **Scan** on the FlipCurve page
(lewisdn.com/flipcurve, or localhost while testing), it loads a handful of eBay and Vinted
pages **in your own browser, signed in as you**, and hands them to the page:

* eBay: 2 newest-listed pages + 2 recently-sold pages (sold results need you signed in to eBay)
* Vinted: 1 newest catalogue page + a few item pages (to date listings) + any re-checks

It pauses 1.5 s between pages, only runs when you press Scan, and stops on any sign-in or
bot-check page (it never tries to get around one). Only lewisdn.com and localhost can talk to it.

Extension ID (fixed by the `key` in manifest.json): `fdibajhmjjbjcdfafdofkoedbkhbkfcb`

## Install (testing)

1. Chrome → `chrome://extensions` → turn on **Developer mode** (top right)
2. **Load unpacked** → choose this `extension/` folder
3. Check the card shows ID `fdibajhmjjbjcdfafdofkoedbkhbkfcb`
4. Make sure you're signed in to eBay in this Chrome

The toolbar icon shows the last eBay check: nothing = fine, `!` = sign in to eBay, `x` = eBay refused.

## Protocol (for the web page)

```js
const EXT = "fdibajhmjjbjcdfafdofkoedbkhbkfcb";
chrome.runtime.sendMessage(EXT, { type: "ping" }, (r) => { /* r = {ok, version, ebayState} or undefined if not installed */ });

const port = chrome.runtime.connect(EXT);
port.onMessage.addListener((m) => { /* {type:"progress", done, total, label} ... then {type:"result", ok, pages, stopped} */ });
port.postMessage({ type: "collect", queries: { ebay: "...", vinted: "..." }, ebayPages: 2, vintedPages: 1,
                   vintedItemSamples: 4, recheckVintedIds: [] });
```

Then POST `{item, pages}` to the backend's `/api/v1/scan-pages`.
