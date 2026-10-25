# FlipCurve helper (Chrome extension)

A search-only helper with no screens. When you press **Scan** on the FlipCurve page
(lewisdn.com/flipcurve, or localhost while testing), it loads a handful of eBay and Vinted
pages **in your own browser, signed in as you**, and hands them to the page:

* eBay: up to 4 newest-listed pages + up to 4 recently-sold pages, 240 listings each, stopping
  early on the last page (sold results need you signed in to eBay)
* Vinted: up to 8 newest catalogue pages (stops at the last) + 4 item pages (to date listings) + any re-checks

eBay and Vinted are loaded side by side: eBay pages 1.5 s apart, Vinted pages 4.5 s apart (Vinted
slows down if asked too often). A request that stalls for 20 s ends that site's part of the scan
with the pages it already got. The popup shows how the last scan went.

**Background re-checks** (switch in the toolbar popup, on by default): every 2 hours while Chrome
is open, the helper asks the FlipCurve server which Vinted listings from earlier scans are due
(only ones that matched the item, first after 3 h, then at most every 6 h, for 30 days), re-opens
up to 20 of them 6 s apart, and reports sold / still up / removed. If Vinted declines, it stops
and waits 12 hours. The server address is the one the FlipCurve page last used.

It pauses 1.5 s between pages, only runs when you press Scan, and stops on any sign-in or
bot-check page (it never tries to get around one). Only lewisdn.com and localhost can talk to it.

Extension ID (fixed by the `key` in manifest.json): `fdibajhmjjbjcdfafdofkoedbkhbkfcb`

## Install (testing)

1. Chrome → `chrome://extensions` → turn on **Developer mode** (top right)
2. **Load unpacked** → choose this `extension/` folder
3. Check the card shows ID `fdibajhmjjbjcdfafdofkoedbkhbkfcb`
4. Make sure you're signed in to eBay in this Chrome

The toolbar icon shows the last eBay check: nothing = fine, `!` = sign in to eBay, `x` = eBay refused.
Click it for the background re-check switch and the last run's results.

## Protocol (for the web page)

```js
const EXT = "fdibajhmjjbjcdfafdofkoedbkhbkfcb";
chrome.runtime.sendMessage(EXT, { type: "ping" }, (r) => { /* r = {ok, version, ebayState} or undefined if not installed */ });

const port = chrome.runtime.connect(EXT);
port.onMessage.addListener((m) => { /* {type:"progress", done, total, label}, {type:"page", page} (one per page),
                                      then {type:"result", ok, sent, stopped} */ });
port.postMessage({ type: "collect", queries: { ebay: "...", vinted: "..." }, ebayPages: 4, vintedPages: 8,
                   vintedItemSamples: 8, recheckVintedIds: [], api: "https://api.lewisdn.com" });
```

Then POST `{item, pages, stopped}` to the backend's `/api/v1/scan-pages` (gzip the body and send
`Content-Encoding: gzip`; pages are several MB).
