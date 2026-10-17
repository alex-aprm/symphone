"use strict";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const COLS = ["id", "name", "mime", "taken", "modified", "size", "w", "h", "bucket", "path", "duration", "orientation", "gen"];
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtSize = n => n == null ? "" : n < 1024 ? n + " B" : n < 1 << 20 ? (n / 1024).toFixed(0) + " KB" : n < 1 << 30 ? (n / 1048576).toFixed(1) + " MB" : (n / 1073741824).toFixed(1) + " GB";
const fmtDur = ms => { const s = Math.round(ms / 1000); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };
const store = { get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { } } };
const G = {   // Segoe Fluent Icons
  phone: "", lock: "", unlock: "", wifi: "", usb: "", bt: "", off: "", refresh: "",
  folder: "", file: "", photo: "", video: "", music: "", doc: "", pkg: "", zip: "",
  check: "", close: "", more: "", add: "", rename: "", star: "", del: "", spinner: "",
  bell: "", pc: "",
};

async function api(path, body, method) {
  const opt = { method: method || (body !== undefined ? "POST" : "GET"), headers: { "X-PhoneSync": "1" } };
  if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  let r, d = {};
  try { r = await fetch(path, opt); } catch { toast("Symphone isn't responding"); return { ok: false }; }
  try { d = await r.json(); } catch { }
  if (!r.ok || d.ok === false) toast(d.error || d.message || `Error ${r.status}`);
  return d;
}
let toastTimer;
function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.hidden = false;
  t.style.animation = "none"; t.offsetHeight; t.style.animation = "";
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.hidden = true, 3200);
}

/* ---------------------------------------------------------------- theme (follows the system) */
let OS = "windows";
// macOS / Linux: Windows-only features hide (.win-only, .no-mac), wording comes from data-mac / data-linux
function applyOs(os) {
  if (os === OS) return;
  OS = os;
  const r = document.documentElement;
  r.dataset.os = os;
  $$(`[data-${os}]`).forEach(e => e.innerHTML = e.dataset[os]);
  if (os !== "windows" && STEPS.includes("extras")) STEPS.splice(STEPS.indexOf("extras"), 1);
}
async function applyTheme() {
  try {
    const t = await (await fetch("/api/theme")).json();
    const r = document.documentElement;
    applyOs(t.platform || "windows");
    r.dataset.theme = t.dark ? "dark" : "light";
    r.classList.toggle("mica", !!t.mica);          // the window's Mica material shows through (Transparency effects on)
    for (const [k, v] of Object.entries({ "--accent": t.accent, "--a-light1": t.light1, "--a-light2": t.light2, "--a-light3": t.light3,
      "--a-dark1": t.dark1, "--a-dark2": t.dark2 })) if (v) r.style.setProperty(k, v);
  } catch { }
}
window.addEventListener("focus", applyTheme);
setInterval(applyTheme, 15000);

/* ---------------------------------------------------------------- tabs */
function showTab(name) {
  if (!document.getElementById("tab-" + name)) name = "status";
  $$("nav.tabs > button[data-tab]").forEach(b => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tab").forEach(t => t.classList.toggle("active", t.id === "tab-" + name));
  store.set("tab2", name);
  if (name === "photos") layoutGrid();
  if (name === "files" && !fs.loaded) fsLoad(fs.path);
  if (name === "apps" && !apps.loaded) appsLoad();
  if (name === "settings") settingsLoad();
  if (name === "status") controlsLoad();
}
$$("nav.tabs > button[data-tab]").forEach(b => b.onclick = () => showTab(b.dataset.tab));
const tabActive = n => $("#tab-" + n).classList.contains("active");

/* ---------------------------------------------------------------- sidebar: phone, battery, connection, lock */
let state = {};
function batterySvg(pct, charging) {
  const w = Math.max(0, Math.min(100, pct)) / 100 * 15;
  return `<svg viewBox="0 0 22 12" fill="none" stroke="currentColor"><rect x=".5" y=".5" width="18.5" height="11" rx="2.5"/>
    <path d="M20.5 4v4" stroke-linecap="round"/><rect x="2.3" y="2.3" width="${w.toFixed(2)}" height="7.4" rx="1" fill="currentColor" stroke="none"/>
    ${charging ? `<path d="M10.6 1.6 7.4 6.6h3l-1.2 3.8 3.4-5.2h-3z" fill="var(--bg)" stroke="var(--bg)" stroke-width=".6" stroke-linejoin="round"/>
      <path d="M10.6 1.6 7.4 6.6h3l-1.2 3.8 3.4-5.2h-3z" fill="currentColor" stroke="none"/>` : ""}</svg>`;
}
function renderState(s) {
  state = s;
  const d = s.device;
  $("#emptyState").hidden = !!s.paired;
  $("#statusBody").hidden = !s.paired;
  $("#devName").textContent = d ? d.name : "No phone yet";
  $("#devModel").textContent = d ? (d.model || "Android phone") : "Add a phone to get started";
  $("#devChev").hidden = !s.paired;
  setPhonePicture(s.active || "");
  // without wireless debugging apps open on the phone's own screen, shown in a casting window
  const adbOk = !!(s.adb && (s.adb.usb || s.adb.wifi));
  $("#appsHint").textContent = adbOk ? "Opens in its own window — works while the phone is locked · Shift+click: on the phone's screen"
    : "Opens on the phone and shows its screen here (the phone asks to allow screen casting) · own windows while locked: Settings › Setup assistant › App windows";
  // battery
  const b = $("#battery");
  b.hidden = !(d && d.battery != null && d.battery >= 0 && d.connected);
  if (!b.hidden) { b.innerHTML = batterySvg(d.battery, d.charging) + `<span>${d.battery}%</span>`; b.title = d.charging ? "Charging" : "Battery"; }
  // connection
  const c = $("#conn"), rc = $("#reconnect");
  if (!d) { c.innerHTML = ""; rc.hidden = true; }
  else if (d.connected) {
    const via = d.via === "USB" ? [G.usb, "USB"] : [G.wifi, "Wi‑Fi"];
    const both = d.links.filter(l => l.healthy).length > 1;
    c.className = "conn"; c.innerHTML = `<span class="ic">${via[0]}</span><span>Connected · ${both ? "USB + Wi‑Fi" : via[1]}</span>`;
    c.title = d.links.filter(l => l.healthy).map(l => `${l.name} ${l.rtt} ms`).join(" · ");
    rc.hidden = true;
  } else {
    c.className = "conn off"; c.innerHTML = `<span class="ic">${G.off}</span><span>Not connected — searching</span>`; c.title = "";
    rc.hidden = false;
  }
  // lock / unlock (one button: shows the current state, hover shows the action)
  const lb = $("#lockBtn");
  lb.hidden = !(d && d.connected && d.locked != null);
  if (!lb.hidden && !lb.classList.contains("busy")) {
    const locked = !!d.locked;
    $(".ic-now", lb).textContent = locked ? G.lock : G.unlock;
    $(".lbl-now", lb).textContent = locked ? "Locked" : "Unlocked";
    $(".ic-hover", lb).textContent = locked ? G.unlock : G.lock;
    $(".lbl-hover", lb).textContent = locked ? "Unlock" : "Lock";
    lb.title = locked ? (s.hasPin ? "Unlock the phone (Ctrl+Alt+U)" : "Unlock the phone — save your PIN in Settings first")
      : "Lock the phone (Ctrl+Alt+L)";
    lb.dataset.locked = locked ? 1 : 0;
  }
  renderHero(s);
  renderPhones(s);
  renderDisplay(s);
  // audio toggles
  const running = s.mirror || {};
  $$("[data-audio]").forEach(cb => {
    const w = cb.dataset.audio;
    cb.checked = w === "system" || w === "mic" ? s.audio?.pc_to_phone?.mode === w : w === "bt_sink" ? !!s.audio?.bt_sink : !!running["audio:" + w];
  });
  $("#audioErr").textContent = s.audio?.pc_to_phone?.error || "";
  const vc = s.vcam || {};
  $("#vcamInfo").textContent = !vc.installed ? "Not installed (run vcam\\install.ps1 once as admin)"
    : vc.active ? `In use · ${vc.size[0]}×${vc.size[1]} · ${vc.fps} fps` : `“${(d && d.name) || "Phone"} Camera” for Teams, Zoom, Camera… — starts the phone camera only while in use`;
  renderVcam(vc);
  $("#driveStatus").textContent = s.drive?.mounted ? (s.drive.mode === "letter" ? `Mounted as ${s.drive.letter}`
      : { windows: "Shown under This PC", mac: "Shown in Finder's sidebar", linux: "Shown in the sidebar of Files" }[OS])
    : (s.drive?.error || (s.connected ? "Mounting…" : "Mounts when the phone is connected"));
  const nb = s.nearby || {};
  $("#bleDetail").textContent = nb.ble === "scanning" ? "On — reconnects as soon as your phone is nearby" + (nb.near?.length ? " · phone nearby" : "")
    : nb.ble === "unavailable" ? `Unavailable: ${nb.bleError || "no Bluetooth LE"}` : "Reconnects as soon as your phone is nearby, even before it joins Wi‑Fi";
  $("#wfdDetail").textContent = nb.wfd === "on" ? "Active — hosting a private Wi‑Fi Direct link for your phone"
    : nb.wfd === "unavailable" ? `Unavailable on this PC: ${nb.wfdError}` : "When the phone is nearby but on another network (or none), this PC hosts a private link for it";
  $("#linkDetail").textContent = d ? (d.links || []).map(l => `${l.name}: ${l.healthy ? l.rtt + " ms" : "—"}`).join(" · ")
    + (s.adb?.usb || s.adb?.wifi ? ` · control over ${s.adb.usb ? "USB" : "Wi‑Fi"}` : "") : "—";
  if (pairOpen) renderCandidates();
}
$("#reconnect").onclick = async () => {
  const b = $("#reconnect"); b.classList.add("spin");
  await api("/api/reconnect", {}); setTimeout(() => b.classList.remove("spin"), 2500);
};
$("#lockBtn").onclick = async () => {
  const lb = $("#lockBtn"); const locked = lb.dataset.locked === "1";
  lb.classList.add("busy"); $(".ic-now", lb).textContent = G.spinner; $(".lbl-now", lb).textContent = locked ? "Unlocking…" : "Locking…";
  const r = await api(locked ? "/api/unlock" : "/api/lock", {});
  lb.classList.remove("busy");
  if (locked && r.message && !r.ok) toast(r.message);
  renderState(state);
};

// phone switcher
$("#devBtn").onclick = e => {
  e.stopPropagation();
  const m = $("#devMenu");
  if (!m.hidden) { m.hidden = true; return; }
  const phones = state.phones || [];
  m.innerHTML = phones.map(p => `<button data-switch="${esc(p.id)}" class="${p.id === state.active ? "on" : ""}">
      <span class="ic">${p.id === state.active ? G.check : G.phone}</span><span>${esc(p.name)}</span>
      <small>${p.connected ? "Connected" : "Offline"}</small></button>`).join("")
    + (phones.length ? "<hr>" : "") + `<button data-pair-open><span class="ic">${G.add}</span><span>Add a phone</span></button>`;
  m.hidden = false;
};
document.addEventListener("click", e => {
  if (!e.target.closest("#devMenu")) $("#devMenu").hidden = true;
  const sw = e.target.closest("[data-switch]");
  if (sw) { $("#devMenu").hidden = true; api("/api/device", { op: "switch", id: sw.dataset.switch }); }
});

// Phone picture (the model's photo with the phone's wallpaper, or the user's own): reloaded when the phone changes,
// when the picture is changed and every few minutes (wallpaper).
let picFor = null, picAt = 0, picVer = 0;
function setPhonePicture(id) {
  if (id === picFor && Date.now() - picAt < 5 * 60e3) return;
  picFor = id; picAt = Date.now();
  const q = `dev=${encodeURIComponent(id)}&t=${Math.floor(picAt / 60e3)}-${picVer}`;
  $("#devPic").src = `/api/phone-picture?${q}&h=128`;
  $("#heroPic").src = `/api/phone-picture?${q}&h=300`;
  fetch(`/api/device/picture?dev=${encodeURIComponent(id)}`).then(r => r.json()).then(s => {
    $("#heroPic").title = s.kind === "model" ? `${s.title} — photo: ${s.author}, ${s.license}, via Wikimedia Commons` : "";
  }).catch(() => { });
}
function pictureChanged() { picVer++; picFor = null; setPhonePicture(state.active || ""); renderPhones(state); }

/* ---------------------------------------------------------------- status page */
function renderHero(s) {
  const d = s.device; if (!d) return;
  $("#heroName").textContent = d.name;
  $("#heroModel").textContent = [d.model, d.android ? "Android " + d.android : ""].filter(Boolean).join(" · ");
  const facts = [];
  if (d.connected && d.battery >= 0) facts.push(`<span>${batterySvg(d.battery, d.charging)} ${d.battery}%${d.charging ? " · charging" : ""}</span>`);
  if (d.storageTotal) {
    const used = 1 - d.storageFree / d.storageTotal;
    facts.push(`<span title="${fmtSize(d.storageFree)} free of ${fmtSize(d.storageTotal)}"><span class="ic"></span>
      <span class="meter"><i style="width:${(used * 100).toFixed(0)}%"></i></span> ${fmtSize(d.storageFree)} free</span>`);
  }
  facts.push(d.connected ? `<span><span class="ic">${d.via === "USB" ? G.usb : G.wifi}</span>${(d.links || []).filter(l => l.healthy).map(l => `${l.name} ${l.rtt} ms`).join(" · ")}</span>`
    : `<span><span class="ic">${G.off}</span>Offline${d.ip ? " · last seen at " + esc(d.ip) : ""}</span>`);
  $("#heroFacts").innerHTML = facts.join("");
}

/* ---------------------------------------------------------------- events */
function connectEvents() {
  const es = new EventSource("/api/events");
  es.addEventListener("state", e => renderState(JSON.parse(e.data)));
  es.addEventListener("media_reset", () => mediaLoad());
  es.addEventListener("media_delta", e => mediaDelta(JSON.parse(e.data)));
  es.addEventListener("notifs", e => { notifs.clear(); JSON.parse(e.data).forEach(n => notifs.set(n.key, n)); loadHistory(); });
  es.addEventListener("notif", e => { const n = JSON.parse(e.data); if (!notifs.has(n.key)) freshKeys.add(n.key); notifs.set(n.key, n); renderNotifs(); });
  es.addEventListener("notif_removed", e => {
    const d = JSON.parse(e.data); notifs.delete(d.key);
    if (d.history) history = [d.history, ...history.filter(h => h.key !== d.key)].slice(0, 300);
    renderNotifs();
  });
  es.addEventListener("paired", e => {
    const d = JSON.parse(e.data); toast(`${d.name || "Phone"} paired${d.via ? " over " + d.via : ""}`);
    if (pairOpen) pollSetup(); 
    apps.loaded = false; fs.loaded = false;
  });
  es.onerror = () => { };
}

/* ---------------------------------------------------------------- photos */
const media = { all: [], list: [], byId: new Map(), filter: "all", sel: new Set(), lastIdx: -1, fresh: new Set() };
let tileSize = store.get("tile", 168);
$("#tileSize").value = tileSize;
const rangeFill = el => el.style.setProperty("--p", ((el.value - el.min) / (el.max - el.min) * 100) + "%");
rangeFill($("#tileSize"));
$("#tileSize").oninput = e => { tileSize = +e.target.value; store.set("tile", tileSize); rangeFill(e.target); layoutGrid(); };

async function mediaLoad() {
  try {
    const d = await (await fetch("/api/media")).json();
    media.all = d.rows.map(r => Object.fromEntries(COLS.map((c, i) => [c, r[i]])));
  } catch { media.all = []; }
  media.byId = new Map(media.all.map(m => [m.id, m]));
  media.sel.clear(); updateSel();
  applyFilter();
}
function mediaDelta(d) {
  for (const id of d.removed) { media.byId.delete(id); media.sel.delete(id); }
  for (const r of d.rows) {
    const m = Object.fromEntries(COLS.map((c, i) => [c, r[i]]));
    if (!media.byId.has(m.id)) media.fresh.add(m.id);
    media.byId.set(m.id, m);
  }
  media.all = [...media.byId.values()].sort((a, b) => b.taken - a.taken || b.id - a.id);
  applyFilter();
}
function applyFilter() {
  const f = media.filter;
  media.list = media.all.filter(m =>
    f === "all" ? true : f === "video" ? m.mime.startsWith("video") :
      f === "camera" ? /^DCIM\//.test(m.path) && !/screenshot/i.test(m.path) : /screenshot/i.test(m.path + m.bucket));
  const vids = media.list.filter(m => m.mime.startsWith("video")).length;
  $("#mediaInfo").textContent = `${(media.list.length - vids).toLocaleString()} photos · ${vids.toLocaleString()} videos`;
  layoutGrid();
}
$$("#mediaFilter button").forEach(b => b.onclick = () => {
  $$("#mediaFilter button").forEach(x => x.classList.toggle("on", x === b));
  media.filter = b.dataset.f; applyFilter(); $("#grid").scrollTop = 0;
});

const GAP = 4, HEAD = 44;
let rows = [], tileW = 0, nodes = new Map();
function monthLabel(ts) { return new Date(ts).toLocaleDateString(undefined, { month: "long", year: "numeric" }); }
function layoutGrid() {
  const grid = $("#grid"); const W = grid.clientWidth - 28;
  if (W <= 0) return;
  const cols = Math.max(2, Math.floor((W + GAP) / (tileSize + GAP)));
  tileW = (W - GAP * (cols - 1)) / cols;
  rows = []; let y = 0, cur = null, row = null;
  media.list.forEach((m, idx) => {
    const lbl = monthLabel(m.taken);
    if (lbl !== cur) { cur = lbl; rows.push({ t: "h", y, label: lbl }); y += HEAD; row = null; }
    if (!row || row.items.length === cols) { row = { t: "r", y, items: [] }; rows.push(row); y += tileW + GAP; }
    row.items.push(idx);
  });
  $("#gridInner").style.height = (y + 20) + "px";
  for (const n of nodes.values()) n.remove();
  nodes.clear();
  if (!media.list.length) $("#gridInner").innerHTML = `<div class="empty"><span class="ic">${G.photo}</span>${state.connected ? "No photos yet" : "Photos appear here when your phone is connected"}</div>`;
  else if ($("#gridInner .empty")) $("#gridInner .empty").remove();
  renderGrid();
}
function renderGrid() {
  const grid = $("#grid"); const top = grid.scrollTop - 600, bot = grid.scrollTop + grid.clientHeight + 900;
  let lo = 0, hi = rows.length;
  while (lo < hi) { const mid = (lo + hi) >> 1; if (rows[mid].y + tileW < top) lo = mid + 1; else hi = mid; }
  const want = new Set(); const inner = $("#gridInner");
  const dpr = window.devicePixelRatio || 1; const ts = tileW * dpr <= 256 ? 256 : 512;
  let badge = null;
  for (let i = lo; i < rows.length && rows[i].y < bot; i++) {
    const r = rows[i];
    if (r.t === "h") {
      const key = "h" + r.y; want.add(key);
      if (!nodes.has(key)) { const el = document.createElement("div"); el.className = "month"; el.style.top = r.y + "px"; el.textContent = r.label; inner.append(el); nodes.set(key, el); }
      if (r.y <= grid.scrollTop + 10) badge = r.label;
      continue;
    }
    r.items.forEach((idx, c) => {
      const m = media.list[idx]; const key = m.id; want.add(key);
      let el = nodes.get(key);
      if (!el) {
        el = document.createElement("div"); el.className = "tile"; el.dataset.idx = idx; el.draggable = true;
        if (media.fresh.has(m.id)) { el.classList.add("new"); media.fresh.delete(m.id); }
        const img = new Image(); img.decoding = "async"; img.onload = () => img.classList.add("ld");
        img.src = `/thumb/${m.id}?s=${ts}&v=${m.modified}`; el.append(img);
        if (m.mime.startsWith("video")) el.insertAdjacentHTML("beforeend", `<span class="dur">▶ ${fmtDur(m.duration)}</span>`);
        inner.append(el); nodes.set(key, el);
      }
      el.dataset.idx = idx;
      el.style.cssText = `top:${r.y}px;left:${c * (tileW + GAP)}px;width:${tileW}px;height:${tileW}px`;
      el.classList.toggle("sel", media.sel.has(m.id));
    });
  }
  for (const [k, el] of nodes) if (!want.has(k)) { el.remove(); nodes.delete(k); }
  const db = $("#dateBadge");
  if (!badge) { for (let i = lo; i >= 0; i--) if (rows[i]?.t === "h") { badge = rows[i].label; break; } }
  db.hidden = !badge || grid.scrollTop < 30; db.textContent = badge || "";
}
$("#grid").addEventListener("scroll", () => requestAnimationFrame(renderGrid), { passive: true });
new ResizeObserver(() => layoutGrid()).observe($("#grid"));

$("#gridInner").addEventListener("click", e => {
  const el = e.target.closest(".tile"); if (!el) return;
  const idx = +el.dataset.idx; const m = media.list[idx];
  if (e.shiftKey && media.lastIdx >= 0) {
    const [a, b] = [Math.min(idx, media.lastIdx), Math.max(idx, media.lastIdx)];
    for (let i = a; i <= b; i++) media.sel.add(media.list[i].id);
  } else if (e.ctrlKey || media.sel.size) {
    media.sel.has(m.id) ? media.sel.delete(m.id) : media.sel.add(m.id);
  } else { openViewer(idx); return; }
  media.lastIdx = idx; updateSel(); renderGrid();
});
$("#gridInner").addEventListener("dragstart", e => {
  const el = e.target.closest(".tile"); if (!el) return;
  const m = media.list[+el.dataset.idx];
  e.dataTransfer.setData("DownloadURL", `${m.mime}:${m.name}:${location.origin}/media/${m.id}`);
});
function updateSel() {
  const n = media.sel.size;
  $("#selInfo").textContent = n ? `${n} selected` : "";
  $("#selClear").hidden = $("#selDownload").hidden = !n;
}
$("#selClear").onclick = () => { media.sel.clear(); updateSel(); renderGrid(); };
$("#selDownload").onclick = async () => {
  const ids = [...media.sel]; media.sel.clear(); updateSel(); renderGrid();
  await api("/api/media/download", { ids });
};
document.addEventListener("keydown", e => {
  if (!$("#viewer").hidden) {
    if (e.key === "Escape") closeViewer();
    if (e.key === "ArrowLeft") stepViewer(-1);
    if (e.key === "ArrowRight") stepViewer(1);
    return;
  }
  if (e.key === "Escape") { closePair(); $("#devMenu").hidden = true; }
  if (tabActive("photos") && e.ctrlKey && e.key === "a" && document.activeElement.tagName !== "INPUT") {
    e.preventDefault(); media.list.forEach(m => media.sel.add(m.id)); updateSel(); renderGrid();
  }
});

/* viewer: opens instantly from the cached grid thumbnail, sharpens progressively, pinch/pan with the touchpad */
let vIdx = -1;
const vz = { s: 1, x: 0, y: 0, swipe: 0 };
function gridThumbUrl(m) { const dpr = window.devicePixelRatio || 1; return `/thumb/${m.id}?s=${tileW * dpr <= 256 ? 256 : 512}&v=${m.modified}`; }
function bigThumbUrl(m) { return `/thumb/${m.id}?s=1280&v=${m.modified}`; }
function openViewer(idx) { vIdx = idx; $("#viewer").hidden = false; showViewer(); }
function closeViewer() { $("#viewer").hidden = true; $("#vStage").innerHTML = ""; }
function stepViewer(d) { const n = vIdx + d; if (n >= 0 && n < media.list.length) { vIdx = n; showViewer(); } }
function applyZoom() {
  const w = $("#vWrap"); if (w) w.style.transform = `translate(${vz.x}px, ${vz.y}px) scale(${vz.s})`;
  $("#viewer").classList.toggle("zoomed", vz.s > 1.01);
}
function resetZoom() { vz.s = 1; vz.x = 0; vz.y = 0; applyZoom(); }
function zoomAt(factor, cx, cy) {
  const stage = $("#vStage").getBoundingClientRect();
  const px = cx - stage.left - stage.width / 2, py = cy - stage.top - stage.height / 2;
  const ns = Math.min(12, Math.max(1, vz.s * factor));
  const k = ns / vz.s;
  vz.x = px - (px - vz.x) * k; vz.y = py - (py - vz.y) * k; vz.s = ns;
  if (ns === 1) { vz.x = 0; vz.y = 0; }
  applyZoom();
}
function showViewer() {
  const m = media.list[vIdx]; const stage = $("#vStage");
  resetZoom();
  $("#vTitle").textContent = `${m.name} · ${new Date(m.taken).toLocaleString()} · ${m.w}×${m.h} · ${fmtSize(m.size)}`;
  if (m.mime.startsWith("video")) {
    stage.innerHTML = `<div id="vWrap" class="v-wrap"><video controls autoplay src="/media/${m.id}" poster="${gridThumbUrl(m)}"></video></div>`;
  } else {
    stage.innerHTML = `<div id="vWrap" class="v-wrap"><img id="vImg" src="${gridThumbUrl(m)}" draggable="false"></div>`;
    const swap = url => { const im = new Image(); im.src = url; im.decode().then(() => { if (media.list[vIdx] === m) $("#vImg").src = url; }).catch(() => { }); return im; };
    swap(bigThumbUrl(m));
    if (!/heic|heif|dng/i.test(m.mime)) setTimeout(() => { if (media.list[vIdx] === m) swap(`/media/${m.id}`); }, 150);
  }
  for (const d of [1, -1, 2]) { const n = media.list[vIdx + d]; if (n && !n.mime.startsWith("video")) new Image().src = bigThumbUrl(n); }
}
$("#vStage").addEventListener("wheel", e => {
  e.preventDefault();
  if (e.ctrlKey) zoomAt(Math.exp(-e.deltaY * 0.01), e.clientX, e.clientY);
  else if (vz.s > 1.01) { vz.x -= e.deltaX; vz.y -= e.deltaY; applyZoom(); }
  else {
    vz.swipe += e.deltaX;
    if (Math.abs(vz.swipe) > 160) { stepViewer(vz.swipe > 0 ? 1 : -1); vz.swipe = 0; }
    clearTimeout(vz.t); vz.t = setTimeout(() => vz.swipe = 0, 250);
  }
}, { passive: false });
$("#vStage").addEventListener("dblclick", e => { if (vz.s > 1.01) resetZoom(); else zoomAt(2.5, e.clientX, e.clientY); });
const vPtrs = new Map(); let vPinch = null;
$("#vStage").addEventListener("pointerdown", e => {
  if (e.target.tagName === "VIDEO") return;
  $("#vStage").setPointerCapture(e.pointerId); vPtrs.set(e.pointerId, { x: e.clientX, y: e.clientY });
  if (vPtrs.size === 2) { const [p, q] = [...vPtrs.values()]; vPinch = Math.hypot(p.x - q.x, p.y - q.y); }
});
$("#vStage").addEventListener("pointermove", e => {
  const prev = vPtrs.get(e.pointerId); if (!prev) return;
  const cur = { x: e.clientX, y: e.clientY }; vPtrs.set(e.pointerId, cur);
  if (vPtrs.size === 2 && vPinch) {
    const [p, q] = [...vPtrs.values()]; const d = Math.hypot(p.x - q.x, p.y - q.y);
    zoomAt(d / vPinch, (p.x + q.x) / 2, (p.y + q.y) / 2); vPinch = d;
  } else if (vPtrs.size === 1 && vz.s > 1.01) { vz.x += cur.x - prev.x; vz.y += cur.y - prev.y; applyZoom(); }
});
const vUp = e => { vPtrs.delete(e.pointerId); if (vPtrs.size < 2) vPinch = null; };
$("#vStage").addEventListener("pointerup", vUp); $("#vStage").addEventListener("pointercancel", vUp);
$("#vClose").onclick = closeViewer; $("#vPrev").onclick = () => stepViewer(-1); $("#vNext").onclick = () => stepViewer(1);
$("#vSave").onclick = () => api("/api/media/download", { ids: [media.list[vIdx].id] });
$("#vOpen").onclick = () => api(`/api/media/${media.list[vIdx].id}/open`, {});

/* ---------------------------------------------------------------- files */
const fs = { path: store.get("fsPath", "/"), items: [], sel: new Set(), loaded: false, last: -1 };
async function fsLoad(path) {
  let r;
  try { r = await fetch(`/api/ls?path=${encodeURIComponent(path)}`); } catch { }
  if (!r || !r.ok) { $("#fsList").innerHTML = `<div class="empty"><span class="ic">${G.off}</span>Phone not reachable</div>`; return; }
  const d = await r.json();
  if (!d.items) { if (path !== "/") return fsLoad("/"); toast("Can't open that folder"); return; }
  fs.loaded = true; fs.path = path; store.set("fsPath", path); fs.sel.clear(); fs.last = -1;
  fs.items = d.items.sort((a, b) => (b.d - a.d) || a.n.localeCompare(b.n, undefined, { numeric: true, sensitivity: "base" }));
  const parts = path.split("/").filter(Boolean);
  $("#crumbs").innerHTML = `<button data-p="/">${esc(state.device?.name || "Phone")}</button>` + parts.map((p, i) =>
    `<span class="ic"></span><button data-p="/${parts.slice(0, i + 1).map(esc).join("/")}">${esc(p)}</button>`).join("");
  $("#fsList").innerHTML = fs.items.length ? fs.items.map((it, i) => `<div class="fs-row" data-i="${i}">
      <span class="fi ${it.d ? "dir" : ""}">${it.d ? "" : fileIcon(it.n)}</span><span>${esc(it.n)}</span>
      <span class="sz">${it.d ? "" : fmtSize(it.s)}</span><span class="dt">${new Date(it.m).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}</span></div>`).join("")
    : `<div class="empty"><span class="ic">${G.folder}</span>This folder is empty — drop files here to copy them to the phone</div>`;
  fsSelUpdate();
}
function fileIcon(n) {
  const e = n.split(".").pop().toLowerCase();
  return /^(jpe?g|png|gif|webp|heic|dng|bmp)$/.test(e) ? G.photo : /^(mp4|mkv|mov|webm|3gp)$/.test(e) ? G.video
    : /^(mp3|m4a|flac|ogg|wav|opus|aac)$/.test(e) ? G.music : e === "apk" ? G.pkg : /^(zip|rar|7z|tar|gz)$/.test(e) ? G.zip
    : /^(pdf|docx?|xlsx?|pptx?|txt|md|csv)$/.test(e) ? G.doc : G.file;
}
const joinP = (a, b) => (a === "/" ? "" : a) + "/" + b;
$("#crumbs").onclick = e => { const b = e.target.closest("button"); if (b) fsLoad(b.dataset.p); };
$("#fsUp").onclick = () => { if (fs.path !== "/") fsLoad(fs.path.replace(/\/[^/]+$/, "") || "/"); };
$("#fsList").addEventListener("click", e => {
  const row = e.target.closest(".fs-row"); if (!row) return;
  const i = +row.dataset.i;
  if (e.shiftKey && fs.last >= 0) { for (let k = Math.min(i, fs.last); k <= Math.max(i, fs.last); k++) fs.sel.add(k); }
  else if (e.ctrlKey) { fs.sel.has(i) ? fs.sel.delete(i) : fs.sel.add(i); }
  else { fs.sel.clear(); fs.sel.add(i); }
  fs.last = i; fsSelUpdate();
});
$("#fsList").addEventListener("dblclick", e => {
  const row = e.target.closest(".fs-row"); if (!row) return;
  const it = fs.items[+row.dataset.i];
  if (it.d) fsLoad(joinP(fs.path, it.n)); else fsDownload([joinP(fs.path, it.n)]);
});
function fsSelUpdate() {
  $$(".fs-row").forEach(r => r.classList.toggle("sel", fs.sel.has(+r.dataset.i)));
  $("#fsDownload").disabled = !fs.sel.size;
  $("#fsDownload span:last-child").textContent = fs.sel.size > 1 ? `Download (${fs.sel.size})` : "Download";
}
const fsDownload = paths => api("/api/files/download", { paths });
$("#fsDownload").onclick = () => fsDownload([...fs.sel].map(i => joinP(fs.path, fs.items[i].n)));
$("#fsUpload").onclick = async () => {
  const { paths } = await api("/api/pick", { kind: "files" });
  if (!paths?.length) return;
  await api("/api/send", { paths, dir: fs.path });
  fsLoad(fs.path);
};
let dragDepth = 0;
const filesTab = $("#tab-files");
filesTab.addEventListener("dragenter", e => { if (e.dataTransfer.types.includes("Files")) { dragDepth++; $("#dropHint").hidden = false; } });
filesTab.addEventListener("dragleave", () => { if (--dragDepth <= 0) { dragDepth = 0; $("#dropHint").hidden = true; } });
filesTab.addEventListener("dragover", e => e.preventDefault());
filesTab.addEventListener("drop", async e => {
  e.preventDefault(); dragDepth = 0; $("#dropHint").hidden = true;
  const entries = [...e.dataTransfer.items].map(i => i.webkitGetAsEntry && i.webkitGetAsEntry()).filter(Boolean);
  const files = [];
  async function walk(entry, prefix) {
    if (entry.isFile) files.push([await new Promise((res, rej) => entry.file(res, rej)), prefix + entry.name]);
    else {
      const reader = entry.createReader(); let batch;
      do { batch = await new Promise((res, rej) => reader.readEntries(res, rej)); for (const c of batch) await walk(c, prefix + entry.name + "/"); } while (batch.length);
    }
  }
  for (const en of entries) await walk(en, "");
  if (!files.length) return;
  const fd = new FormData();
  for (const [f, rel] of files) fd.append("f", f, rel);
  await fetch(`/api/files/upload?dir=${encodeURIComponent(fs.path)}`, { method: "POST", body: fd, headers: { "X-PhoneSync": "1" } });
  fsLoad(fs.path);
});
document.addEventListener("dragover", e => e.preventDefault());
document.addEventListener("drop", e => e.preventDefault());

/* ---------------------------------------------------------------- apps */
const apps = { items: [], loaded: false };
async function appsLoad() {
  try { const r = await fetch("/api/apps"); if (!r.ok) throw 0; apps.items = (await r.json()).items; apps.loaded = true; }
  catch { apps.items = []; }
  renderApps();
}
function renderApps() {
  const q = $("#appSearch").value.toLowerCase(), sys = $("#appSystem").checked;
  const phoneTile = q ? "" : `<div class="app special" data-phone="1" title="The whole phone in a window — works while locked, the phone's screen stays off">
      <span class="phone-ic ic">${G.phone}</span><span>Phone screen</span></div>`;
  const list = apps.items.filter(a => (sys || !a.system) && (!q || a.label.toLowerCase().includes(q) || a.pkg.includes(q)));
  $("#appGrid").innerHTML = phoneTile + list.map(a => `<div class="app" data-pkg="${esc(a.pkg)}" data-label="${esc(a.label)}" title="${esc(a.pkg)}">
      <img loading="lazy" src="/icon/${esc(a.pkg)}" alt=""><span>${esc(a.label)}</span></div>`).join("")
    + (!apps.loaded ? `<div class="empty" style="grid-column:1/-1">Apps appear here when your phone is connected</div>` : "");
}
$("#appSearch").oninput = renderApps; $("#appSystem").onchange = renderApps;
$("#appGrid").onclick = async e => {
  const a = e.target.closest(".app"); if (!a) return;
  if (a.dataset.phone) { toast("Opening the phone screen…"); return api("/api/mirror", { kind: "phone" }); }
  toast(`Opening ${a.dataset.label}…`);
  await api("/api/launch", { pkg: a.dataset.pkg, label: a.dataset.label, window: !e.shiftKey });
};

/* ---------------------------------------------------------------- notifications */
const notifs = new Map(), freshKeys = new Set();
let history = [];
async function loadHistory() {
  try { const d = await (await fetch("/api/notifications")).json(); notifs.clear(); d.items.forEach(n => notifs.set(n.key, n)); history = d.history || []; } catch { }
  renderNotifs();
}
function ago(ts) {
  const s = (Date.now() - ts) / 1000;
  return s < 60 ? "now" : s < 3600 ? `${Math.floor(s / 60)} min ago` : s < 86400 ? new Date(ts).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })
    : new Date(ts).toLocaleDateString(undefined, { day: "numeric", month: "short" });
}
const appIcon = n => `/icon/${encodeURIComponent(n.pkg)}`;
function renderNotifs() {
  const list = [...notifs.values()].filter(n => !n.ongoing || n.call).sort((a, b) => (b.call - a.call) || b.when - a.when);
  // the badge counts what you can act on: ongoing / non-dismissable ones (USB debugging, a car app…) don't add up
  const unread = list.filter(n => n.clearable || n.call).length;
  $("#notifCount").hidden = !unread; $("#notifCount").textContent = unread;
  if (document.activeElement?.dataset?.ri) return;      // don't wipe a reply being typed
  $("#notifList").innerHTML = list.length ? list.map(n => {
    const body = n.messages?.length ? n.messages.slice(-4).map(m => (m.from ? `<b>${esc(m.from)}:</b> ` : "") + esc(m.text)).join("\n") : esc(n.text);
    const reply = n.actions.find(a => a.reply);
    const isNew = freshKeys.delete(n.key);
    return `<div class="notif ${n.call ? "call" : ""} ${isNew ? "new" : ""}" data-key="${esc(n.key)}" title="Open in a window">
      <div class="hd"><img src="${appIcon(n)}" alt=""><span>${esc(n.app)}</span><span class="when">${ago(n.when)}</span></div>
      ${n.clearable ? `<button class="icon-btn x" data-dismiss="1" title="Dismiss"><span class="ic">${G.close}</span></button>` : ""}
      <div class="bd">${n.hasIcon ? `<img class="avatar" src="/notif-icon?key=${encodeURIComponent(n.key)}" alt="" onerror="this.remove()">` : ""}
        <div style="min-width:0;flex:1"><div class="ti">${esc(n.title)}</div><div class="tx">${body}</div></div></div>
      ${n.call || n.actions.some(a => !a.reply) ? `<div class="acts">${n.call ? `<button class="btn accent" data-callpc="1">Answer on PC</button>` : ""}
        ${n.actions.filter(a => !a.reply).map(a => `<button class="btn" data-ai="${a.i}">${esc(a.title)}</button>`).join("")}</div>` : ""}
      ${reply ? `<div class="reply"><input class="text" placeholder="Reply" data-ri="${reply.i}"><button class="btn accent" data-send="${reply.i}" title="Send"><span class="ic"></span></button></div>` : ""}
      </div>`;
  }).join("") : `<div class="empty"><span class="ic">${G.bell}</span>No new notifications</div>`;
  $("#historySep").hidden = !history.length; $("#clearHistory").hidden = !history.length;
  $("#notifHistory").innerHTML = history.slice(0, 150).map((n, i) => `<div class="notif past" data-hi="${i}" title="Open ${esc(n.app)} in a window">
      <div class="hd"><img src="${appIcon(n)}" alt=""><span>${esc(n.app)}</span><span class="when">${ago(n.when)}</span></div>
      <div class="ti">${esc(n.title)}</div><div class="tx">${esc((n.text || "").slice(0, 300))}</div></div>`).join("");
  renderMuted();
}
$("#notifHistory").addEventListener("click", e => {
  const c = e.target.closest("[data-hi]"); if (!c) return;
  const n = history[+c.dataset.hi];
  api("/api/launch", { pkg: n.pkg, label: n.app, window: true });
});
$("#clearHistory").onclick = async () => { await api("/api/notifications/history", {}, "DELETE"); history = []; renderNotifs(); };
$("#notifList").addEventListener("click", async e => {
  const card = e.target.closest(".notif"); if (!card) return;
  const key = card.dataset.key; const t = e.target.closest("button,input") || e.target;
  if (t.tagName === "INPUT") return;
  if (t.dataset.ai) return api("/api/notif/act", { key, op: "action", index: +t.dataset.ai });
  if (t.dataset.dismiss) { card.style.opacity = .4; return api("/api/notif/act", { key, op: "dismiss" }); }
  if (t.dataset.callpc) return api("/api/call", { op: "answer_pc" });
  if (t.dataset.send) {
    const inp = $(`input[data-ri="${t.dataset.send}"]`, card); const text = inp.value.trim(); if (!text) return;
    inp.disabled = true;
    const r = await api("/api/notif/act", { key, op: "action", index: +t.dataset.send, text });
    inp.disabled = false;
    if (r.ok !== false) { inp.value = ""; inp.blur(); toast("Sent"); }
    return;
  }
  api("/api/notif/act", { key, op: "open" });
});
$("#notifList").addEventListener("keydown", e => {
  if (e.key === "Enter" && e.target.dataset.ri) $(`[data-send="${e.target.dataset.ri}"]`, e.target.closest(".notif")).click();
});
setInterval(() => { if (tabActive("status")) renderNotifs(); }, 60000);

/* ---------------------------------------------------------------- actions */
document.addEventListener("click", async e => {
  const b = e.target.closest("[data-act],[data-snap],[data-open],[data-pickfor],[data-pair-open]");
  if (!b) return;
  if (b.dataset.pairOpen !== undefined) { $("#devMenu").hidden = true; return openPair(); }
  if (b.dataset.act) {
    const a = b.dataset.act;
    if (a === "phone") { toast("Opening the phone screen…"); api("/api/mirror", { kind: "phone" }); }
    if (a === "camera") api("/api/mirror", { kind: "camera", facing: state.vcam?.facing || "back" });
    if (a === "drive") api("/api/drive/open", {});
    if (a === "send") {
      const { paths } = await api("/api/pick", { kind: "files" });
      if (paths?.length) api("/api/send", { paths });
    }
  }
  if (b.dataset.snap) { await api("/api/capture", { facing: b.dataset.snap, unlock: false }); toast("Click! The photo shows up in Photos in a moment"); }
  if (b.dataset.open) api("/api/open", { setting: b.dataset.open });
  if (b.dataset.pickfor) {
    const { paths } = await api("/api/pick", { kind: "folder" });
    if (paths?.length) { await api("/api/settings", { [b.dataset.pickfor]: paths[0] }); settingsLoad(); }
  }
});
// Windows camera: which phone camera, rotation (manual + follow the phone), mirror
function renderVcam(vc) {
  const sel = $("#vcamCamera"), cams = vc.cameras || [];
  const opts = cams.length ? cams.map(c => [c.id, c.label + (cams.filter(x => x.label === c.label).length > 1 ? ` (${c.id})` : "")])
    : [["", vc.facing === "front" ? "Front" : "Back"]];
  const html = opts.map(([v, t]) => `<option value="${esc(v)}">${esc(t)}</option>`).join("");
  if (sel.dataset.html !== html) { sel.innerHTML = html; sel.dataset.html = html; }
  if (document.activeElement !== sel) sel.value = vc.camera || (cams.find(c => c.facing === (vc.facing || "back")) || {}).id || "";
  $$("#vcamRotate button").forEach(b => b.classList.toggle("on", +b.dataset.r === (vc.rotate || 0)));
  $("#vcamAuto").checked = !!vc.autorotate;
  $("#vcamMirror").checked = !!vc.mirror;
}
$("#vcamCamera").onchange = e => api("/api/vcam", { camera: e.target.value });
$$("#vcamRotate button").forEach(b => b.onclick = () => api("/api/vcam", { rotate: +b.dataset.r }));
$("#vcamAuto").onchange = e => api("/api/vcam", { autorotate: e.target.checked });
$("#vcamMirror").onchange = e => api("/api/vcam", { mirror: e.target.checked });
fetch("/api/vcam").catch(() => { });          // fills the camera list once the phone is connected
$$("[data-audio]").forEach(cb => cb.onchange = () =>
  api("/api/audio", { what: cb.dataset.audio, on: cb.checked, virtual_mic: !!cb.dataset.vmic }));

/* ---------------------------------------------------------------- quick settings (copy of the phone's drawer) */
const QS = [
  ["wifi", "", "Wi‑Fi"], ["bluetooth", "", "Bluetooth"], ["data", "", "Mobile data"], ["airplane", "", "Airplane mode"],
  ["torch", "", "Flashlight"], ["dnd", "", "Do not disturb"], ["location", "", "Location"], ["rotate", "", "Auto‑rotate"],
  ["saver", "", "Battery saver"], ["dark", "", "Dark theme"], ["nfc", "", "NFC"], ["autobright", "", "Auto brightness"],
];
let qsState = null, qsTimer = null, qsBusy = 0;
async function controlsLoad() {
  clearTimeout(qsTimer);
  if (!tabActive("status") || !state.paired) return;
  if (!qsBusy && state.connected) {
    try {
      const r = await fetch("/api/controls");
      const d = await r.json();
      if (r.ok) { qsState = d; renderControls(); $("#qsErr").textContent = ""; }
      else $("#qsErr").textContent = d.error || "Phone not reachable";
    } catch { }
  } else if (!state.connected) { $("#qsTiles").innerHTML = ""; $("#qsSliders").innerHTML = ""; $("#qsErr").textContent = "Available when the phone is connected"; }
  qsTimer = setTimeout(controlsLoad, 4000);
}
function renderControls() {
  const t = qsState.toggles || {};
  $("#qsTiles").innerHTML = QS.map(([k, icon, label]) => {
    const on = k === "torch" ? qsState.torch : t[k]?.on;
    if (k !== "torch" && !t[k]) return "";
    return `<button class="qs-tile ${on ? "on" : ""}" data-qs="${k}" data-on="${on ? 1 : 0}" title="${esc(label)}"><span class="face"><span class="ic">${icon}</span></span><span>${esc(label)}</span></button>`;
  }).join("");
  if (document.activeElement?.type === "range" && document.activeElement.closest("#qsSliders")) return;
  const v = qsState.volumes || {};
  const sl = (id, icon, label, val, min, max) => `<label class="qs-slider" title="${label}"><span class="ic">${icon}</span>
    <input type="range" data-sl="${id}" min="${min}" max="${max}" value="${val}"><b>${Math.round((val - min) / (max - min) * 100)}</b></label>`;
  $("#qsSliders").innerHTML = [
    qsState.brightness != null ? sl("brightness", "", "Brightness", qsState.brightness, 1, 255) : "",
    ...[["media", "", "Media volume"], ["ring", "", "Ring volume"], ["alarm", "", "Alarm volume"], ["call", "", "Call volume"]]
      .filter(([k]) => v[k]).map(([k, i, l]) => sl("vol:" + k, i, l, v[k].value, v[k].min, v[k].max)),
  ].join("");
  $$("#qsSliders input").forEach(rangeFill);
}
$("#qsTiles").addEventListener("click", async e => {
  const b = e.target.closest("[data-qs]"); if (!b) return;
  const on = b.dataset.on !== "1";
  b.classList.toggle("on", on); b.dataset.on = on ? 1 : 0;
  qsBusy++;
  try { await api("/api/controls", { toggle: b.dataset.qs, on }); } finally { qsBusy--; }
  setTimeout(controlsLoad, 600);
});
let slTimer;
$("#qsSliders").addEventListener("input", e => {
  const el = e.target; if (!el.dataset.sl) return;
  rangeFill(el);
  el.nextElementSibling.textContent = Math.round((el.value - el.min) / (el.max - el.min) * 100);
  clearTimeout(slTimer);
  slTimer = setTimeout(() => {
    const k = el.dataset.sl;
    api("/api/controls", k === "brightness" ? { brightness: +el.value } : { volume: k.slice(4), value: +el.value });
  }, 120);
});

/* ---------------------------------------------------------------- settings */
let settings = {};
async function settingsLoad() {
  try { settings = await (await fetch("/api/settings")).json(); } catch { return; }
  $$("[data-set]").forEach(el => {
    const v = settings[el.dataset.set];
    if (document.activeElement === el) return;
    if (el.type === "checkbox") el.checked = !!v; else el.value = v ?? "";
  });
  const dl = $("#driveLetter");
  if (!dl.options.length) dl.innerHTML = "DEFGHIJKLMNOPQRSTUVWXYZ".split("").map(c => `<option>${c}</option>`).join("");
  dl.value = settings.drive_letter; dl.hidden = settings.drive_mode !== "letter";
  $("#pin").placeholder = settings.hasPin ? "Saved ••••" : "PIN";
  $("#clearPin").hidden = !settings.hasPin;
  $("#dlDir").textContent = settings.downloads_dir; $("#impDir").textContent = "To " + settings.import_dir;
  renderSize();
  renderMuted();
}
$$("[data-set]").forEach(el => el.onchange = async () => {
  let v = el.type === "checkbox" ? el.checked : el.value;
  if (el.dataset.num) v = +v;
  await api("/api/settings", { [el.dataset.set]: v });
  if (el.dataset.set === "drive_mode") settingsLoad();
});
$("#driveLetter").onchange = e => api("/api/settings", { drive_letter: e.target.value });
$("#savePin").onclick = async () => { const v = $("#pin").value.trim(); if (!v) return; await api("/api/settings", { pin: v }); $("#pin").value = ""; toast("PIN saved"); settingsLoad(); };
$("#clearPin").onclick = async () => { await api("/api/settings", { pin: "" }); toast("PIN removed"); settingsLoad(); };
$("#pin").onkeydown = e => { if (e.key === "Enter") $("#savePin").click(); };
function renderMuted() {
  const muted = new Set(settings.toast_muted_apps || []);
  const seen = new Map([...notifs.values(), ...history].map(n => [n.pkg, n.app]));
  for (const p of muted) if (!seen.has(p)) seen.set(p, p);
  $("#mutedApps").innerHTML = [...seen].sort((a, b) => a[1].localeCompare(b[1])).map(([p, label]) =>
    `<div class="at"><img src="/icon/${encodeURIComponent(p)}" alt=""><span>${esc(label)}</span>
      <label class="switch"><input type="checkbox" data-mute="${esc(p)}" ${muted.has(p) ? "" : "checked"}><span></span></label></div>`).join("")
    || `<div class="at t3">Apps show up here once they've sent a notification</div>`;
}
$("#mutedApps").onchange = e => {
  const c = e.target.closest("[data-mute]"); if (!c) return;
  const muted = new Set(settings.toast_muted_apps || []);
  c.checked ? muted.delete(c.dataset.mute) : muted.add(c.dataset.mute);
  settings.toast_muted_apps = [...muted]; api("/api/settings", { toast_muted_apps: settings.toast_muted_apps });
};

// paired phones
function renderPhones(s) {
  const el = $("#phoneList");
  if (el.contains(document.activeElement) && document.activeElement.tagName === "INPUT") return;
  el.innerHTML = (s.phones || []).map(p => `<div class="row phone-row" data-pid="${esc(p.id)}">
      <img class="row-pic" alt="" draggable="false" src="/api/phone-picture?dev=${encodeURIComponent(p.id)}&h=80&t=${picVer}">
      <span class="row-text"><b>${esc(p.name)}</b><small><span class="dot ${p.connected ? "on" : ""}"></span>
        ${p.connected ? "Connected · " + esc(p.via) : "Offline"}${p.model ? " · " + esc(p.model) : ""}${p.master === false ? " · another PC is its main PC" : ""}</small></span>
      ${p.id === s.active ? `<span class="tag acc">Showing</span>` : `<button class="btn" data-pact="switch">Show</button>`}
      <button class="icon-btn" data-pact="menu" title="More"><span class="ic">${G.more}</span></button></div>`).join("")
    || `<div class="row"><span class="ic row-ic">${G.phone}</span><span class="row-text"><b>No phones yet</b><small>Add one below</small></span></div>`;
}
$("#phoneList").addEventListener("click", async e => {
  const b = e.target.closest("[data-pact]"); if (!b) return;
  const row = b.closest("[data-pid]"); const id = row.dataset.pid; const p = (state.phones || []).find(x => x.id === id);
  if (b.dataset.pact === "switch") return api("/api/device", { op: "switch", id });
  if (b.dataset.pact === "menu") {
    e.stopPropagation();
    $$(".row-menu").forEach(m => m.remove());
    const m = document.createElement("div"); m.className = "menu row-menu";
    m.style.cssText = `position:fixed;left:auto;right:${innerWidth - b.getBoundingClientRect().right}px;top:${b.getBoundingClientRect().bottom + 4}px;width:260px`;
    const pic = await fetch(`/api/device/picture?dev=${encodeURIComponent(id)}`).then(r => r.json()).catch(() => ({}));
    m.innerHTML = `<button data-m="rename"><span class="ic">${G.rename}</span><span>Rename</span></button>
      <button data-m="picture"><span class="ic">\uEB9F</span><span>Change picture…</span></button>
      ${pic.kind === "custom" ? `<button data-m="autopic"><span class="ic">\uE72C</span><span>Use the automatic picture</span></button>` : ""}
      ${p.master === false ? `<button data-m="master"><span class="ic">${G.star}</span><span>Make this PC its main PC</span></button>` : ""}
      <hr><button data-m="forget"><span class="ic">${G.del}</span><span>Remove this phone</span></button>`;
    document.body.append(m);
    m.onclick = async ev => {
      const it = ev.target.closest("[data-m]"); if (!it) return; m.remove();
      if (it.dataset.m === "rename") {
        const t = $(".row-text b", row); const inp = document.createElement("input"); inp.className = "text"; inp.value = p.name;
        t.replaceWith(inp); inp.focus(); inp.select();
        const done = async ok => { inp.onblur = null; if (ok && inp.value.trim()) await api("/api/device", { op: "rename", id, name: inp.value.trim() }); renderPhones(state); };
        inp.onkeydown = k => { if (k.key === "Enter") done(true); if (k.key === "Escape") done(false); };
        inp.onblur = () => done(true);
      }
      if (it.dataset.m === "master") api("/api/device", { op: "master", id });
      if (it.dataset.m === "picture") {
        const f = document.createElement("input"); f.type = "file"; f.accept = "image/*";
        f.onchange = async () => {
          if (!f.files[0]) return;
          const r = await fetch(`/api/device/picture?dev=${encodeURIComponent(id)}`, { method: "POST",
            headers: { "X-PhoneSync": "1" }, body: f.files[0] }).then(r => r.json()).catch(() => ({}));
          if (r.ok) { pictureChanged(); toast("Picture changed"); } else toast(r.error || "Couldn't use that picture");
        };
        f.click();
      }
      if (it.dataset.m === "autopic") {
        await fetch(`/api/device/picture?dev=${encodeURIComponent(id)}`, { method: "DELETE", headers: { "X-PhoneSync": "1" } });
        pictureChanged();
      }
      if (it.dataset.m === "forget") {
        if (row.dataset.confirm) return;
        row.dataset.confirm = 1;
        const bar = document.createElement("div"); bar.className = "row";
        bar.innerHTML = `<span class="ic row-ic" style="color:var(--bad)">${G.del}</span><span class="row-text"><b>Remove ${esc(p.name)}?</b><small>It disconnects from this PC; pair again any time.</small></span>
          <button class="btn" data-no>Cancel</button><button class="btn accent" data-yes>Remove</button>`;
        row.after(bar);
        bar.onclick = async ev2 => {
          if (ev2.target.closest("[data-yes]")) { await api("/api/device", { op: "forget", id }); toast(`${p.name} removed`); }
          if (ev2.target.closest("[data-yes],[data-no]")) { bar.remove(); delete row.dataset.confirm; }
        };
      }
    };
    setTimeout(() => document.addEventListener("click", () => m.remove(), { once: true }), 0);
  }
});

/* ---------------------------------------------------------------- pairing dialog */
// First-run setup wizard (also "Add a phone"): get the app → pair → phone permissions → wireless debugging →
// extras → done. Polls /api/setup while open; steps tick themselves off as things happen.
const STEPS = ["app", "pair", "phone", "wireless", "extras", "done"];
let pairOpen = false, setupStep = "app", setupTimer = 0, setupSt = {}, phonesAtOpen = 0, extrasSel = null;
// wantNew: opened to add another phone (a new pairing completes the "connect" step), not to finish setting up one
async function openSetup(step, wantNew = false) {
  $("#setup").hidden = false; pairOpen = true; renderSetup.advanced = false; extrasSel = null;
  phonesAtOpen = wantNew ? (state.phones || []).length : 0;
  await pollSetup();
  showStep(step || (setupSt.connected ? "phone" : "app"));
  clearInterval(setupTimer); setupTimer = setInterval(pollSetup, 2000);
}
const openPair = () => openSetup((state.phones || []).length ? "pair" : "app", (state.phones || []).length > 0);
function closePair() {
  if (!pairOpen) return;
  pairOpen = false; $("#setup").hidden = true; clearInterval(setupTimer);
  api("/api/pair/stop", {}); api("/api/setup", { op: "adb-pair-stop" });
}
async function showStep(st) {
  setupStep = st;
  $$(".setup-page").forEach(p => p.classList.toggle("cur", p.dataset.s === st));
  const i = STEPS.indexOf(st);
  $("#setupBack").hidden = i === 0;
  $("#setupSkip").hidden = !["wireless", "extras"].includes(st);
  $("#setupNext").textContent = st === "done" ? "Start using Symphone" : "Next";
  if (st === "app" && !$("#apkQr").innerHTML) {
    const r = await api("/api/setup", { op: "apk-qr" });
    $("#apkQr").innerHTML = r.svg || ""; $("#apkUrl").textContent = r.url || "";
  }
  if (st === "pair") {
    $("#qr").innerHTML = `<span class="ic" style="font-size:24px;animation:spin 1s linear infinite">${G.spinner}</span>`;
    const r = await api("/api/pair/start", {});
    if (r.svg) $("#qr").innerHTML = r.svg;
    $("#pairHint").textContent = r.ips ? `This PC: ${r.ips.replace(/,/g, ", ")} · the code is valid for 10 minutes` : "";
    renderCandidates();
  }
  if (st === "wireless" && !setupSt.usb && !setupSt.wireless && !["pairing", "connecting", "setting-up", "done"].includes(setupSt.adb_pair?.state)) {
    const r = await api("/api/setup", { op: "adb-pair" });
    $("#adbQr").innerHTML = r.svg || "";
  }
  renderSetup();
}
async function pollSetup() {
  try { setupSt = await (await fetch("/api/setup")).json(); } catch { return; }
  renderSetup();
}
function renderSetup() {
  const s = setupSt, phones = state.phones || [];
  const paired = phones.length > phonesAtOpen;
  const done = {
    app: paired || s.usb, pair: paired && s.connected,
    phone: (s.checklist || []).length > 0 && (s.checklist || []).filter(x => x.required).every(x => x.ok),
    wireless: !!(s.usb || s.wireless || s.adb_pair?.state === "done"),
    extras: Object.values(s.extras || {}).every(Boolean), done: false,
  };
  $$("#setupSteps li").forEach(li => {
    li.classList.toggle("cur", li.dataset.s === setupStep); li.classList.toggle("ok", !!done[li.dataset.s] && li.dataset.s !== setupStep);
  });
  // step 1: USB path
  const usb = $("#usbLine"); usb.hidden = !s.usb;
  if (s.usb) usb.lastElementChild.textContent = paired ? "Phone connected over USB and set up" : "Phone connected over USB — setting up the app…";
  // step 2: paired
  const pl = $("#pairedLine"); pl.hidden = !paired;
  if (paired) pl.lastElementChild.textContent = `Connected to ${phones[phones.length - 1]?.name || "your phone"}`;
  // step 3: the phone's checklist
  const cl = s.checklist;
  $("#phoneChecklist").innerHTML = !cl ? `<div class="row"><span class="ic row-ic">${G.spinner}</span><span class="row-text"><b>Waiting for the phone…</b>
      <small>Open Symphone on the phone (version 2.6 or newer shows its checklist here)</small></span></div>`
    : cl.map(x => `<div class="row check-row"><span class="ic row-ic ${x.ok ? "ok" : x.ok === false ? "no" : ""}">${x.ok ? "\uE73E" : x.ok === false ? "\uE7BA" : "\uE9CE"}</span>
      <span class="row-text"><b>${esc(x.title)}</b>${x.hint ? `<small>${esc(x.hint)}</small>` : ""}</span>
      <span class="tag ${x.required ? "req" : ""}">${x.required ? "needed" : "optional"}</span></div>`).join("");
  // step 4: wireless debugging
  const wl = $("#wirelessLine"), ap = s.adb_pair || {};
  const wdone = s.usb || s.wireless || ap.state === "done";
  wl.hidden = !wdone; $("#wirelessPair").hidden = !!wdone;
  if (wdone) wl.lastElementChild.textContent = s.usb ? "Set up over USB" : "Wireless debugging is set up";
  $("#adbState").textContent = { waiting: "Waiting for the phone…", pairing: "Pairing…", connecting: "Connecting…",
    "setting-up": "Setting up the phone…", done: "Done", error: ap.error || "Something went wrong" }[ap.state] || "Waiting for the phone…";
  // step 5: extras
  const ex = s.extras || {};
  const items = [["winfsp", "Phone storage in Explorer", "A native drive (WinFsp); without it Symphone uses a slower WebDAV fallback"],
    ["vbcable", "Phone microphone in Windows", "Use the phone as a microphone in any app (VB-CABLE)"],
    ["display", "Phone as a second monitor", "The side display driver"],
    ["webcam", "Phone camera as a webcam", "For Teams, Zoom, Camera and browsers"]];
  if (!extrasSel) extrasSel = new Set(items.filter(([k]) => !ex[k]).map(([k]) => k));
  const el = $("#extrasList");
  if (!el.contains(document.activeElement)) el.innerHTML = items.map(([k, t, d]) => `<label class="row check-row">
      <span class="ic row-ic ${ex[k] ? "ok" : ""}">${ex[k] ? "\uE73E" : "\uE710"}</span><span class="row-text"><b>${t}</b><small>${d}</small></span>
      ${ex[k] ? `<span class="tag">installed</span>` : `<input type="checkbox" data-extra="${k}" ${extrasSel.has(k) ? "checked" : ""}>`}</label>`).join("");
  $("#extrasInstall").disabled = !items.some(([k]) => !ex[k]);
  // auto-advance when a step completes by itself
  if (setupStep === "pair" && done.pair && !renderSetup.advanced) { renderSetup.advanced = true; setTimeout(() => showStep("phone"), 1200); }
}
$("#setupNext").onclick = async () => {
  const i = STEPS.indexOf(setupStep);
  if (setupStep === "done") { await api("/api/setup", { op: "done" }); closePair(); return; }
  showStep(STEPS[i + 1]);
};
$("#setupSkip").onclick = () => showStep(STEPS[STEPS.indexOf(setupStep) + 1]);
$("#setupBack").onclick = () => showStep(STEPS[Math.max(0, STEPS.indexOf(setupStep) - 1)]);
$("#setupAgain") && ($("#setupAgain").onclick = () => openSetup("app"));
$("#setupLater").onclick = async () => { await api("/api/setup", { op: "done" }); closePair(); };
$("#setupSteps").onclick = e => { const li = e.target.closest("li"); if (li) showStep(li.dataset.s); };
$("#extrasList").onchange = e => { const k = e.target.dataset.extra; if (k) e.target.checked ? extrasSel.add(k) : extrasSel.delete(k); };
$("#extrasInstall").onclick = async () => {
  const r = await api("/api/setup", { op: "extras", items: [...extrasSel] });
  $("#extrasMsg").textContent = r.ok ? "Windows asks for permission, then installs in a separate window. This list updates when it's done."
    : (r.error || "Couldn't start the installer");
};
function renderCandidates() {
  const el = $("#candidates");
  if (el.contains(document.activeElement)) return;
  const c = state.candidates || [];
  el.innerHTML = c.length ? c.map(x => `<div class="row"><span class="ic row-ic">${G.phone}</span>
      <span class="row-text"><b>${esc(x.name)}</b><small>${esc(x.ip)}</small></span>
      <input class="text" maxlength="6" size="8" placeholder="6-digit code" data-ip="${esc(x.ip)}"><button class="btn accent" data-pair="${esc(x.ip)}">Pair</button></div>`).join("")
    : `<p class="t3">Looking for phones with Symphone on this network… The code is shown in the Symphone app.</p>`;
}
document.addEventListener("click", async e => {
  const b = e.target.closest("[data-pair]"); if (!b) return;
  const ip = b.dataset.pair, code = $(`input[data-ip="${ip}"]`).value.trim();
  const r = await api("/api/pair", { ip, code });
  if (r.ok) { toast("Paired"); pollSetup(); }
});

/* ---------------------------------------------------------------- side display */
function renderDisplay(s) {
  const sc = s.sidecar || {};
  const on = ["starting", "streaming", "reconnecting"].includes(sc.state);
  $("#displayLive").hidden = !on;
  const sw = $("#scOn");
  if (!sw.dataset.busy) sw.checked = on;
  $("#scInfo").textContent = on ? `${sc.state === "streaming" ? "On" : sc.state[0].toUpperCase() + sc.state.slice(1) + "…"}${sc.fps ? ` · ${Math.round(sc.fps)} fps` : ""}${sc.latency_ms?.p50 ? ` · ${Math.round(sc.latency_ms.p50)} ms` : ""}${sc.link ? ` · ${sc.link}` : ""}`
    : sc.state === "error" ? (sc.error || "Couldn't start") : s.connected ? "Off" : "Connect your phone first";
  $("#dhPhone").classList.toggle("on", on);
  $("#dhTitle").textContent = `Use ${s.device?.name || "your phone"} as a second display`;
  const orient = settings.sidecar_orientation || "landscape";
  $$("#scOrient button").forEach(b => b.classList.toggle("on", b.dataset.o === orient));
  $("#dhPhone").classList.toggle("portrait", orient === "portrait");
}
$("#scOn").onchange = async e => {
  const sw = e.target; sw.dataset.busy = 1;
  if (sw.checked) {
    $("#scInfo").textContent = "Starting…";
    const scale = settings.sidecar_size_auto ? null : (settings.sidecar_size || 300);
    const r = await api("/api/sidecar", { op: "start", orientation: settings.sidecar_orientation || "landscape", scale });
    if (r.state === "error") toast(r.error || "Side display couldn't start");
  } else await api("/api/sidecar", { op: "stop" });
  delete sw.dataset.busy;
};
$$("#scOrient button").forEach(b => b.onclick = async () => {
  settings.sidecar_orientation = b.dataset.o;
  await api("/api/settings", { sidecar_orientation: b.dataset.o });
  renderDisplay(state);
  if ($("#scOn").checked) { await api("/api/sidecar", { op: "stop" }); await api("/api/sidecar", { op: "start", orientation: b.dataset.o, scale: settings.sidecar_size_auto ? null : (settings.sidecar_size || 300) }); }
});
// Text size: 100-200% is Windows' own scaling; above that the side display runs at a lower resolution (softer text).
const SIZES = [100, 125, 150, 175, 200, 250, 300, 350, 400, 450, 500, 550, 600];
function renderSize() {
  const auto = !!settings.sidecar_size_auto, v = settings.sidecar_size || 300;
  $("#scAuto").checked = auto;
  const sl = $("#scSize"); sl.disabled = auto;
  let i = SIZES.indexOf(v); if (i < 0) i = SIZES.indexOf(300);
  if (document.activeElement !== sl) sl.value = i;
  showSize(auto ? null : SIZES[+sl.value]);
}
function showSize(v) {
  $("#scSizeVal").textContent = v ? v + "%" : "Auto";
  $("#scSizeHint").textContent = !v ? "Windows chooses the size for the phone display"
    : v > 200 ? "Above 200% the phone display uses a lower resolution, so text is larger but softer"
    : "Size of text and apps on the phone display (DPI)";
}
let sizeTimer = 0;
function applySize() {
  clearTimeout(sizeTimer);
  sizeTimer = setTimeout(() => {
    const scale = settings.sidecar_size_auto ? 0 : settings.sidecar_size;
    api("/api/settings", { sidecar_size: settings.sidecar_size, sidecar_size_auto: settings.sidecar_size_auto });
    if ($("#scOn").checked) api("/api/sidecar", { op: "scale", scale });
  }, 350);
}
$("#scSize").oninput = e => { settings.sidecar_size = SIZES[+e.target.value]; showSize(settings.sidecar_size); applySize(); };
$("#scAuto").onchange = e => { settings.sidecar_size_auto = e.target.checked; renderSize(); applySize(); };
$("#scDisplaySettings").onclick = () => api("/api/open", { path: "ms-settings:display" });

/* ---------------------------------------------------------------- boot */
(async function boot() {
  await applyTheme();
  connectEvents();
  try { renderState(await (await fetch("/api/state")).json()); } catch { }
  try { settings = await (await fetch("/api/settings")).json(); } catch { }
  renderDisplay(state);
  showTab(location.hash.slice(1) || store.get("tab2", "status"));
  fetch("/api/setup").then(r => r.json()).then(s => { if (!s.done && !s.phones) openSetup("app"); }).catch(() => { });
  addEventListener("hashchange", () => location.hash.length > 1 && showTab(location.hash.slice(1)));
  await mediaLoad();
  await loadHistory();
})();
