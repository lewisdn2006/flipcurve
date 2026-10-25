// Popup for the FlipCurve helper: background re-check switch and status.
const $ = (id) => document.getElementById(id);
const ago = (t) => {
  const m = Math.round((Date.now() - t) / 60000);
  return m < 1 ? "just now" : m < 60 ? `${m} min ago` : `${Math.round(m / 60)} h ago`;
};
const until = (t) => {
  const m = Math.round((t - Date.now()) / 60000);
  return m <= 0 ? "soon" : m < 60 ? `in ${m} min` : `in ${Math.round(m / 60)} h`;
};

function row(k, v, cls = "") {
  const d = document.createElement("div");
  const a = document.createElement("span"); a.textContent = k;
  const b = document.createElement("span"); b.textContent = v; if (cls) b.className = cls;
  d.append(a, b);
  return d;
}

async function refresh() {
  const s = await chrome.runtime.sendMessage({ type: "recheck-status" });
  $("enabled").checked = s.enabled;
  const box = $("status");
  box.replaceChildren();
  box.append(row("Server", s.api.replace(/^https?:\/\//, "")));
  if (s.running && s.progress) box.append(row("Now", `checking ${s.progress.done} of ${s.progress.total}`));
  const l = s.last;
  if (l) {
    if (l.error) box.append(row(`Last run (${ago(l.at)})`, l.error, "err"));
    else if (l.skipped) box.append(row(`Last run (${ago(l.at)})`, l.skipped));
    else {
      box.append(row(`Last run (${ago(l.at)})`, l.checked ? `${l.checked} checked` : "nothing due yet"));
      if (l.checked) box.append(row("Found", `${l.sold || 0} sold · ${l.active || 0} still up · ${l.deleted || 0} removed`));
      if (l.stopped) box.append(row("Vinted", l.stopped === "refused" ? "declined, pausing 12 h" : "slowed down, will try next run", "err"));
    }
  } else {
    box.append(row("Last run", "not yet"));
  }
  const sc = s.lastScan;
  if (sc && sc.startedAt) {
    const how = sc.crash ? `stopped: ${sc.crash}` : sc.finishedAt
      ? (sc.stopped && Object.keys(sc.stopped).length ? `ended early (${Object.entries(sc.stopped).map(([k, v]) => `${k}: ${v.reason}`).join(", ")})` : `${sc.sent} pages`)
      : `stopped at ${sc.step}/${sc.steps}: ${sc.label}`;
    box.append(row(`Last scan (${ago(sc.startedAt)})`, how, sc.crash || !sc.finishedAt ? "err" : ""));
  }
  if (s.pausedUntil > Date.now()) box.append(row("Paused", until(s.pausedUntil)));
  else if (s.enabled && s.next) box.append(row("Next run", until(s.next)));
  $("now").disabled = s.running;
}

$("enabled").addEventListener("change", async (e) => {
  await chrome.runtime.sendMessage({ type: "recheck-toggle", enabled: e.target.checked });
  refresh();
});
$("now").addEventListener("click", async () => {
  $("now").disabled = true;
  $("now").textContent = "Checking…";
  const timer = setInterval(refresh, 1500);
  await chrome.runtime.sendMessage({ type: "recheck-now" });
  clearInterval(timer);
  $("now").textContent = "Check now";
  refresh();
});
refresh();
