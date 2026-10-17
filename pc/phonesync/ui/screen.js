'use strict';
// Phone screen viewer: scrcpy video over a websocket -> WebCodecs -> canvas, and input back to the phone.
// URL: screen.html?kind=screen|app|camera[&pkg=..&label=..][&facing=back][&mode=auto|main|virtual][&max_size=..]

const P = new URLSearchParams(location.search);
const KIND = P.get('kind') || 'screen';
const $ = s => document.querySelector(s);
const stage = $('#stage'), view = $('#view'), canvas = $('#screen');
const ctx = canvas.getContext('2d', { alpha: false, desynchronized: true });
document.body.classList.add('kind-' + KIND);
document.title = P.get('title') || (KIND === 'app' ? (P.get('label') || P.get('pkg') || 'App')
  : KIND === 'camera' ? `Camera (${P.get('facing') || 'back'})` : 'Phone');

const POINTER_GENERIC_FINGER = -2;
// Virtual displays: the viewer asks for its picture area in CSS px; the server turns that into display pixels at the
// phone's own density (apps that size themselves from the phone's screen metrics break on other densities).
const MAX_DECODE_QUEUE = 8;            // frames; ~130 ms at 60 fps
const BACKLOG_RESYNC_MS = 500;         // ...for this long before resyncing: a burst from Wi-Fi decodes in a few ms
// Playout delay cap (ms) for smoothing network jitter; ?jitter=0 draws every frame as soon as it is decoded
const JITTER_MAX = Math.max(0, +(P.get('jitter') ?? 60) || 0);
const MAX_HELD = 4;                    // decoded frames waiting for their display time (decoders have few buffers)
const A_DOWN = 0, A_UP = 1, A_MOVE = 2;

let ws = null, decoder = null, codecStr = '', lastConfig = null, pendingConfig = null, needKey = true;
let configuredAt = 0, backlogSince = 0;
let vw = 0, vh = 0, meta = {}, gotFrame = false, closedByError = false, resetAsked = 0;
let fit = { x: 0, y: 0, w: 0, h: 0 };
const vz = { z: 1, tx: 0, ty: 0 };              // local view zoom (magnifier)
let magnifier = false;
const recvTimes = new Map();                    // pts -> PC receive time (ms) for latency stats
const st = { frames: 0, bytes: 0, lat: [], dropped: 0, decErr: 0, resets: 0, skipped: 0 };

// ------------------------------------------------------------------ status / toasts
function setStatus(text, error = false, retry = false) {
  const el = $('#status');
  el.hidden = !text;
  el.classList.toggle('error', error);
  $('#statusText').textContent = text || '';
  $('#retry').hidden = !retry;
}
function toast(text, ms = 1600) {
  const t = document.createElement('div');
  t.className = 'toast'; t.textContent = text;
  document.body.appendChild(t);
  setTimeout(() => { t.style.opacity = 0; setTimeout(() => t.remove(), 300); }, ms);
}
$('#retry').onclick = () => { retries = 0; connect(); };
$('#toPhone').onclick = () => send({ t: 'cmd', c: 'to_phone' });

// ------------------------------------------------------------------ connection
function send(o) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(o)); }

function connect() {
  if (ws) { try { ws.onclose = null; ws.close(); } catch { } }
  closeDecoder();
  gotFrame = false; closedByError = false; needKey = true; lastConfig = pendingConfig = null;
  setStatus('Connecting…');
  const q = new URLSearchParams(P);
  q.delete('title'); q.delete('label');
  // Virtual displays are created at the window's size and then follow it: the picture always fills the window.
  if (KIND !== 'camera') { const v = vdSize(); q.set('vd', `${v.w}x${v.h}`); }
  ws = new WebSocket(`ws://${location.host}/ws/screen?${q}`);
  ws.binaryType = 'arraybuffer';
  ws.onmessage = e => typeof e.data === 'string' ? onJson(JSON.parse(e.data)) : onPacket(e.data);
  ws.onclose = () => {
    endGestures();
    if (!closedByError) setStatus('Disconnected from the phone', true, true);
    scheduleReconnect();
  };
}

// Transient failures (adb hiccup, phone briefly unreachable, server restart): retry with backoff.
let retries = 0, retryTimer = 0;
function scheduleReconnect() {
  clearTimeout(retryTimer);
  if (retries >= 6) return;
  const delay = [1000, 2000, 3000, 5000, 8000, 13000][retries++];
  retryTimer = setTimeout(() => { if (!ws || ws.readyState === WebSocket.CLOSED) connect(); }, delay);
}

function onJson(m) {
  if (m.type === 'meta') {
    meta = m; fitted = false; setSize(m.width, m.height);
    if (document.hasFocus()) send({ t: 'focus', on: true });
    document.body.classList.toggle('virtual', (m.display || '').startsWith('virtual'));
    if (m.codec !== 'h264') setStatus(`Codec ${m.codec} is not supported by the viewer (use h264)`, true);
    else setStatus('Waiting for video…');
  } else if (m.type === 'size') {
    const flipped = vw && (vw > vh) !== (m.width > m.height);
    setSize(m.width, m.height);
    if (flipped && !meta.flex) fitWindowToStream();
  } else if (m.type === 'secure') {
    $('#secure').hidden = !m.on;
    if (m.on) $('#secureTitle').textContent = `${KIND === 'app' ? document.title : 'This app'} protects its screen`;
  } else if (m.type === 'content') {
    document.body.classList.toggle('away', !m.shown);
  } else if (m.type === 'away') {
    document.body.classList.add('away');
  } else if (m.type === 'back') {
    document.body.classList.remove('away');
  } else if (m.type === 'clipboard') {
    navigator.clipboard?.writeText(m.text).catch(() => { });
  } else if (m.type === 'error') {
    closedByError = true;
    setStatus(m.message + (m.log && m.log.length ? '\n' + m.log.join('\n') : ''), true, true);
  }
}

// ------------------------------------------------------------------ video
function codecFromConfig(data) {
  // find the SPS (nal type 7) after an Annex B start code: avc1.PPCCLL
  for (let i = 0; i + 4 < data.length; i++) {
    if (data[i] === 0 && data[i + 1] === 0 && data[i + 2] === 1 && (data[i + 3] & 0x1f) === 7) {
      const h = b => b.toString(16).padStart(2, '0');
      return 'avc1.' + h(data[i + 4]) + h(data[i + 5]) + h(data[i + 6]);
    }
  }
  return 'avc1.640028';
}

function closeDecoder() {
  if (decoder && decoder.state !== 'closed') { try { decoder.close(); } catch { } }
  decoder = null;
  dropHeld();
}

function makeDecoder(codec) {
  closeDecoder();
  codecStr = codec;
  decoder = new VideoDecoder({
    output: onFrame,
    error: e => {
      console.warn('decoder error', e); st.decErr++;
      decoder = null; needKey = true; requestKeyframe();
    },
  });
  configuredAt = performance.now(); backlogSince = 0;
  pacer.reset();                                     // a new encoder session may restart its timestamps
  decoder.configure({ codec, optimizeForLatency: true, hardwareAcceleration: P.get('hw') || 'no-preference' });
  needKey = true;
}

function requestKeyframe(force = false) {
  const now = performance.now();
  if (force || now - resetAsked > 1000) { resetAsked = now; send({ t: 'cmd', c: 'reset' }); }
}

function sameBytes(a, b) {
  if (!a || !b || a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
  return true;
}

function onPacket(buf) {
  const dv = new DataView(buf);
  const flags = dv.getUint8(0), tRecv = dv.getFloat64(1), pts = Number(dv.getBigUint64(9));
  const data = new Uint8Array(buf, 17);
  st.bytes += buf.byteLength;
  if (flags & 1) {                                   // config packet (SPS/PPS)
    const cfg = data.slice();
    const codec = codecFromConfig(cfg);
    if (!decoder || decoder.state === 'closed' || codec !== codecStr || !sameBytes(cfg, lastConfig)) makeDecoder(codec);
    lastConfig = cfg; pendingConfig = cfg;
    return;
  }
  if (!decoder || decoder.state !== 'configured') { if (lastConfig) { makeDecoder(codecStr); pendingConfig = lastConfig; } else return; }
  const key = !!(flags & 2);
  // Latency guard: if the decoder itself can't keep up, throw the backlog away and resume on a fresh keyframe.
  // Keep the (warmed-up) decoder: stop feeding deltas, let it drain, and resume on a keyframe requested now.
  // A short burst (Wi-Fi delivering a stall's worth of frames at once) doesn't count: it decodes in a few ms and
  // the late frames are skipped at presentation, without the freeze of waiting for a keyframe.
  const now = performance.now();
  if (!key && !needKey && decoder.decodeQueueSize > MAX_DECODE_QUEUE) {
    if (!backlogSince) backlogSince = now;
    else if (now - backlogSince > BACKLOG_RESYNC_MS && now - configuredAt > 2000) {
      st.resets++; needKey = true; backlogSince = 0; requestKeyframe(true);
    }
  } else backlogSince = 0;
  if (needKey && !key) { st.dropped++; requestKeyframe(); return; }
  let payload = data;
  if (pendingConfig) {                               // Annex B without description: SPS/PPS go in-band
    payload = new Uint8Array(pendingConfig.length + data.length);
    payload.set(pendingConfig); payload.set(data, pendingConfig.length);
    pendingConfig = null;
  }
  needKey = false;
  recvTimes.set(pts, tRecv);
  if (recvTimes.size > 300) recvTimes.delete(recvTimes.keys().next().value);
  try {
    decoder.decode(new EncodedVideoChunk({ type: key ? 'key' : 'delta', timestamp: pts, data: payload }));
  } catch (e) { console.warn(e); needKey = true; requestKeyframe(); }
}

// ------------------------------------------------------------------ presentation (frame pacing)
// Wi-Fi delivers frames in bursts (airtime contention, power save, retransmissions). Drawing each frame the moment it
// is decoded turns that into visible judder: a stall, then several frames in one refresh. Instead each frame is shown
// at its phone timestamp plus a playout delay that follows the measured jitter (~0 over USB, a few tens of ms on a
// busy Wi-Fi), one per display refresh; frames that are already late are skipped, so a stall never adds latency.
const pacer = {
  win: [],              // [t, offset] of recent frames, offset = PC time - phone timestamp (ms)
  delay: 0,             // current playout delay (ms)
  lastTune: 0,
  reset() { this.win = []; this.delay = 0; this.lastTune = 0; },
  // performance.now() time at which the frame with this timestamp (µs) should be on screen
  due(pts, now) {
    const off = now - pts / 1000, w = this.win;
    w.push([now, off]);
    while (now - w[0][0] > 3000) w.shift();
    if (!JITTER_MAX) return now;
    // the smallest offset is how long an undelayed frame takes (plus the clocks' difference)
    let base = Infinity;
    for (const e of w) if (e[1] < base) base = e[1];
    if (now - this.lastTune > 250 && w.length >= 10) {
      this.lastTune = now;
      const j = w.map(e => e[1] - base).sort((a, b) => a - b);
      const target = Math.min(JITTER_MAX, j[Math.floor(j.length * 0.95)]);
      // grow at once (the next burst is likely), shrink slowly once the link has calmed down
      this.delay = target > this.delay ? target : Math.max(target, this.delay - 2);
    }
    return pts / 1000 + base + this.delay;
  },
};
let held = [], presentRaf = 0;

function dropHeld() {
  for (const h of held) h.frame.close();
  held = [];
}

function onFrame(frame) {
  const t = recvTimes.get(frame.timestamp);
  if (t) { st.lat.push(Date.now() - t); recvTimes.delete(frame.timestamp); }
  const now = performance.now();
  const due = pacer.due(frame.timestamp, now);
  if (due <= now && !held.length) { draw(frame); return; }       // on time (USB, calm Wi-Fi): no added latency
  held.push({ frame, due });
  while (held.length > MAX_HELD) { held.shift().frame.close(); st.skipped++; }
  if (!presentRaf) presentRaf = requestAnimationFrame(present);
}

function present() {
  presentRaf = 0;
  const now = performance.now() + 4;                 // a frame due within ~half a refresh goes into this one
  let pick = -1;
  for (let i = 0; i < held.length; i++) if (held[i].due <= now) pick = i;
  if (pick >= 0) {
    for (let i = 0; i < pick; i++) { held[i].frame.close(); st.skipped++; }   // the newest due frame wins
    const f = held[pick].frame;
    held.splice(0, pick + 1);
    draw(f);
  }
  if (held.length) presentRaf = requestAnimationFrame(present);
}

function draw(frame) {
  const w = frame.displayWidth, h = frame.displayHeight;
  if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
  if (vw !== w || vh !== h) setSize(w, h);
  ctx.drawImage(frame, 0, 0);
  frame.close();
  st.frames++;
  if (!gotFrame) { gotFrame = true; retries = 0; setStatus(''); }
  if (probe) probe.check();
}

// ------------------------------------------------------------------ layout & local zoom
function setSize(w, h) {
  vw = w; vh = h; layout();
  // Fixed-size streams (phone screen, camera): the window takes the stream's shape once, so nothing is letterboxed.
  if (!meta.flex && !fitted && w && h) { fitted = true; fitWindowToStream(true); }
}
let fitted = false;
const areaW = () => stage.clientWidth, areaH = () => stage.clientHeight;
function vdSize() {
  return { w: Math.max(100, Math.round(areaW())), h: Math.max(100, Math.round(areaH())) };
}

function layout() {
  if (!vw || !vh) return;
  const W = areaW(), H = areaH();
  // contain; but a sliver of mismatch (rounding while a flex display catches up) is covered instead of striped
  let s = Math.min(W / vw, H / vh);
  const cover = Math.max(W / vw, H / vh);
  if (cover / s < 1.04) s = cover;
  fit = { w: vw * s, h: vh * s };
  fit.x = (W - fit.w) / 2; fit.y = (H - fit.h) / 2;
  canvas.style.width = fit.w + 'px'; canvas.style.height = fit.h + 'px';
  applyView();
}

function applyView() {
  const W = areaW(), H = areaH();
  if (vz.z <= 1.0001) { vz.z = 1; vz.tx = vz.ty = 0; }
  else {
    const cw = fit.w * vz.z, ch = fit.h * vz.z;
    const clamp = (left, size, avail) => size <= avail ? (avail - size) / 2 : Math.min(0, Math.max(avail - size, left));
    vz.tx = clamp(fit.x + vz.tx, cw, W) - fit.x;
    vz.ty = clamp(fit.y + vz.ty, ch, H) - fit.y;
  }
  view.style.transform = `translate(${fit.x + vz.tx}px, ${fit.y + vz.ty}px) scale(${vz.z})`;
  document.body.classList.toggle('zoomed', vz.z > 1);
  const b = $('#zoomBadge');
  b.hidden = vz.z === 1; b.textContent = `View ${vz.z.toFixed(1)}×`;
}

function zoomViewAt(cx, cy, f) {
  const z2 = Math.min(10, Math.max(1, vz.z * f));
  const px = (cx - fit.x - vz.tx) / vz.z, py = (cy - fit.y - vz.ty) / vz.z;
  vz.tx = cx - fit.x - z2 * px; vz.ty = cy - fit.y - z2 * py; vz.z = z2;
  applyView();
}
// Ask the host to give the window the stream's aspect (plus the control strip): keeps the width (first fit / after
// a resize) or the long side (after a rotation).
let fitUntil = 0;
function fitWindowToStream(keepWidth = false) {
  if (!vw || !vh) return;
  const bar = innerHeight - areaH();
  const maxW = screen.availWidth * 0.9, maxH = screen.availHeight * 0.9 - bar;
  const s = keepWidth ? Math.min(areaW() / vw, maxW / vw, maxH / vh)
    : Math.min(Math.max(areaW(), areaH()) / Math.max(vw, vh), maxW / vw, maxH / vh);
  const w = Math.round(vw * s), h = Math.round(vh * s) + bar;
  if (Math.abs(w - innerWidth) < 3 && Math.abs(h - innerHeight) < 3) return;
  fitUntil = performance.now() + 800;
  // the host adds its own frame size (it knows the native window size; outerWidth doesn't include it)
  send({ t: 'win', w, h, iw: innerWidth, ih: innerHeight });
}
function resetView() { vz.z = 1; applyView(); }
let resizeTimer = 0, snapTimer = 0;
// scrcpy may drop a resize that races a display change: re-ask until the stream has the window's size
let vdTimer = 0;
function requestVd(tries) {
  const v = vdSize();
  send({ t: 'vd', w: v.w, h: v.h });
  clearTimeout(vdTimer);
  if (tries < 3) vdTimer = setTimeout(() => {
    const want = vdSize();          // the stream may be scaled down (max size): compare shapes
    if (vw && vh && Math.abs(vw / vh - want.w / want.h) > 0.02) requestVd(tries + 1);
  }, 1500);
}
addEventListener('resize', () => {
  layout();
  clearTimeout(resizeTimer); clearTimeout(snapTimer);
  if (meta.flex) {
    // the phone relayouts the app for the new window size (short debounce: each resize restarts the capture)
    resizeTimer = setTimeout(() => requestVd(0), 120);
  } else if (performance.now() > fitUntil) {
    snapTimer = setTimeout(() => fitWindowToStream(true), 600);
  }
});

// normalized phone coordinates of a client point (handles the magnifier transform)
function norm(cx, cy) {
  const r = canvas.getBoundingClientRect();
  const x = (cx - r.left) / r.width, y = (cy - r.top) / r.height;
  return { x: Math.min(1, Math.max(0, x)), y: Math.min(1, Math.max(0, y)), inside: x >= 0 && x <= 1 && y >= 0 && y <= 1, r };
}

// ------------------------------------------------------------------ pointer input
const touchIds = new Map();       // browser pointerId -> phone pointer id
let mouseTouch = false, magDrag = null;

function capture(e) { try { stage.setPointerCapture(e.pointerId); } catch { /* synthetic pointer */ } }
function freeId() { for (let i = 0; i < 32; i++) if (![...touchIds.values()].includes(i)) return i; return 31; }

function sendTouch(a, id, e, pressure) {
  const n = norm(e.clientX, e.clientY);
  send({ t: 'touch', a, id, x: n.x, y: n.y, p: pressure ?? 1 });
}

stage.addEventListener('pointerdown', e => {
  stage.focus({ preventScroll: true });
  if (!gotFrame) return;
  // blurred (the app is on the phone or gone): a click brings it back here instead of touching what's behind
  if (document.body.classList.contains('away')) { if (e.button === 0) send({ t: 'cmd', c: 'pull' }); return; }
  if (e.pointerType === 'mouse') {
    if (e.button === 0 && magnifier) {
      magDrag = { x: e.clientX, y: e.clientY, tx: vz.tx, ty: vz.ty };
      document.body.classList.add('dragging');
    } else if (e.button === 0) {
      if (!norm(e.clientX, e.clientY).inside) return;
      mouseTouch = true; sendTouch(A_DOWN, POINTER_GENERIC_FINGER, e);
    } else if (e.button === 2) send({ t: 'key', k: 'BACK', a: 'down' });
    else if (e.button === 1) { e.preventDefault(); send({ t: 'key', k: 'HOME', a: 'down' }); }
    else return;
    capture(e);
    return;
  }
  // touchscreen / pen: real multi-touch, one phone pointer per browser pointer
  if (!norm(e.clientX, e.clientY).inside) return;
  const id = freeId();
  touchIds.set(e.pointerId, id);
  capture(e);
  sendTouch(A_DOWN, id, e, e.pointerType === 'pen' ? (e.pressure || 1) : 1);
});

stage.addEventListener('pointermove', e => {
  if (e.pointerType === 'mouse') {
    if (magDrag) {
      vz.tx = magDrag.tx + e.clientX - magDrag.x; vz.ty = magDrag.ty + e.clientY - magDrag.y; applyView();
    } else if (mouseTouch) {
      for (const ce of (e.getCoalescedEvents?.() || [e])) sendTouch(A_MOVE, POINTER_GENERIC_FINGER, ce);
    }
    return;
  }
  const id = touchIds.get(e.pointerId);
  if (id !== undefined) sendTouch(A_MOVE, id, e, e.pointerType === 'pen' ? (e.pressure || 1) : 1);
});

function pointerEnd(e) {
  if (e.pointerType === 'mouse') {
    if (e.button === 0 && magDrag) { magDrag = null; document.body.classList.remove('dragging'); }
    else if (e.button === 0 && mouseTouch) { mouseTouch = false; sendTouch(A_UP, POINTER_GENERIC_FINGER, e, 0); }
    else if (e.button === 2) send({ t: 'key', k: 'BACK', a: 'up' });
    else if (e.button === 1) send({ t: 'key', k: 'HOME', a: 'up' });
    else if (e.type === 'pointercancel' && mouseTouch) { mouseTouch = false; sendTouch(A_UP, POINTER_GENERIC_FINGER, e, 0); }
    return;
  }
  const id = touchIds.get(e.pointerId);
  if (id !== undefined) { touchIds.delete(e.pointerId); sendTouch(A_UP, id, e, 0); }
}
stage.addEventListener('pointerup', pointerEnd);
stage.addEventListener('pointercancel', pointerEnd);
stage.addEventListener('contextmenu', e => e.preventDefault());
stage.addEventListener('mousedown', e => { if (e.button === 1) e.preventDefault(); });   // no autoscroll
stage.addEventListener('dblclick', e => { if (magnifier) resetView(); });

// ------------------------------------------------------------------ wheel: scroll, touchpad pinch / pan
let pinch = null, pan = null;
function endPinch() { if (pinch) { clearTimeout(pinch.timer); pinch = null; send({ t: 'pinch', ph: 'end' }); } }
function endPan() {
  if (!pan) return;
  clearTimeout(pan.timer);
  if (pan.down) send({ t: 'pan', ph: 'end' });     // never pressed (below touch slop): nothing to release, no tap
  pan = null; panQuietUntil = performance.now() + 220;
}
let panQuietUntil = 0;
const PAN_SLOP = 10;                                 // CSS px of travel before the virtual finger touches down
function endGestures() { endPinch(); endPan(); }

function isMouseWheel(e) {
  // Notched mouse wheels: line mode, or exact multiples of 120 on one axis. Precision touchpads send small pixel deltas.
  if (e.deltaMode === 1) return true;
  return e.deltaX === 0 && e.wheelDeltaY !== 0 && Math.abs(e.wheelDeltaY) % 120 === 0;
}

stage.addEventListener('wheel', e => {
  e.preventDefault();
  if (!gotFrame) return;
  if (document.body.classList.contains('away')) return;
  if (e.altKey || magnifier) {                       // local magnifier
    const f = Math.exp(-(e.ctrlKey ? e.deltaY * 2 : e.deltaY) / 300);
    zoomViewAt(e.clientX, e.clientY, Math.min(1.5, Math.max(0.66, f)));
    return;
  }
  const n = norm(e.clientX, e.clientY);
  if (e.ctrlKey) {                                   // precision-touchpad pinch (or Ctrl+wheel) -> real pinch
    endPan();
    const f = Math.min(1.25, Math.max(0.8, Math.exp(-e.deltaY / 100)));
    if (!pinch) {
      if (!n.inside) return;
      pinch = {}; send({ t: 'pinch', ph: 'begin', x: n.x, y: n.y, s: f });
    } else send({ t: 'pinch', ph: 'update', s: f });
    clearTimeout(pinch.timer); pinch.timer = setTimeout(endPinch, 160);
    return;
  }
  if (isMouseWheel(e) && !pan) {                     // mouse wheel -> Android scroll event
    if (!n.inside) return;
    const k = e.deltaMode === 1 ? 1 / 3 : 1 / 100;
    send({ t: 'scroll', x: n.x, y: n.y, h: -e.deltaX * k, v: -e.deltaY * k });
    return;
  }
  // two-finger touchpad scroll -> one-finger drag that follows the content (lists scroll, maps/photos pan)
  endPinch();
  let dx = -e.deltaX, dy = -e.deltaY;
  if (e.deltaMode === 1) { dx *= 33; dy *= 33; }
  if (e.shiftKey && !dx) { dx = dy; dy = 0; }
  if (!pan) {
    if (!n.inside) return;
    // Momentum tail right after a gesture: tiny deltas would start a new, almost motionless drag = a tap/click.
    if (performance.now() < panQuietUntil && Math.hypot(dx, dy) < 6) { panQuietUntil = performance.now() + 120; return; }
    pan = { down: false, x: n.x, y: n.y, ax: 0, ay: 0 };
  }
  clearTimeout(pan.timer); pan.timer = setTimeout(endPan, 160);
  if (!pan.down) {
    pan.ax += dx; pan.ay += dy;
    if (Math.hypot(pan.ax, pan.ay) < PAN_SLOP) return;
    pan.down = true;
    send({ t: 'pan', ph: 'begin', x: pan.x, y: pan.y });
    dx = pan.ax; dy = pan.ay;
  }
  send({ t: 'pan', ph: 'update', dx: dx / n.r.width, dy: dy / n.r.height });
}, { passive: false });

// ------------------------------------------------------------------ keyboard
const KEYMAP = {
  Enter: 'ENTER', NumpadEnter: 'ENTER', Backspace: 'DEL', Delete: 'FORWARD_DEL', Tab: 'TAB', Escape: 'BACK',
  ArrowUp: 'DPAD_UP', ArrowDown: 'DPAD_DOWN', ArrowLeft: 'DPAD_LEFT', ArrowRight: 'DPAD_RIGHT',
  Home: 'MOVE_HOME', End: 'MOVE_END', PageUp: 'PAGE_UP', PageDown: 'PAGE_DOWN', Insert: 'INSERT',
  AudioVolumeUp: 'VOLUME_UP', AudioVolumeDown: 'VOLUME_DOWN', AudioVolumeMute: 'VOLUME_MUTE',
  MediaPlayPause: 'MEDIA_PLAY_PAUSE',
};
const META_SHIFT = 0x41, META_CTRL = 0x3000, META_ALT = 0x12;
const KEYCODE_A = 29;

function metaOf(e) { return (e.shiftKey ? META_SHIFT : 0) | (e.ctrlKey ? META_CTRL : 0) | (e.altKey ? META_ALT : 0); }

document.addEventListener('keydown', e => {
  if (e.key === 'F2') { toggleStats(); e.preventDefault(); return; }
  if (!gotFrame) return;
  if (e.ctrlKey && !e.altKey && !e.metaKey) {
    const k = e.key.toLowerCase();
    if (k === 'v') return;                                     // handled by the paste event
    if (k === 'c' || k === 'x') { send({ t: 'key', k: k === 'c' ? 'COPY' : 'CUT' }); e.preventDefault(); return; }
    if (k.length === 1 && k >= 'a' && k <= 'z') {
      send({ t: 'key', k: KEYCODE_A + k.charCodeAt(0) - 97, meta: metaOf(e) }); e.preventDefault(); return;
    }
  }
  const name = KEYMAP[e.code === 'NumpadEnter' ? 'NumpadEnter' : e.key];
  if (name) {
    send({ t: 'key', k: name, a: 'down', repeat: e.repeat ? 1 : 0, meta: metaOf(e) });
    e.preventDefault();
    return;
  }
  if ([...e.key].length === 1 && !e.metaKey && (!e.ctrlKey || e.altKey)) {   // printable (AltGr = Ctrl+Alt)
    send({ t: 'text', s: e.key });
    e.preventDefault();
  }
});
document.addEventListener('keyup', e => {
  const name = KEYMAP[e.code === 'NumpadEnter' ? 'NumpadEnter' : e.key];
  if (name && gotFrame) { send({ t: 'key', k: name, a: 'up', meta: metaOf(e) }); e.preventDefault(); }
});
document.addEventListener('paste', e => {
  const text = e.clipboardData?.getData('text/plain');
  if (text) { send({ t: 'clip', s: text, paste: true }); e.preventDefault(); }
});
addEventListener('blur', () => { endGestures(); send({ t: 'focus', on: false }); });
// While this window has the keyboard, text fields on its phone display take PC typing (the phone keyboard stays hidden).
addEventListener('focus', () => send({ t: 'focus', on: true }));

// ------------------------------------------------------------------ toolbar
let uiTimer = 0;
function pokeUI() {
  document.body.classList.add('show-ui');
  clearTimeout(uiTimer);
  uiTimer = setTimeout(() => document.body.classList.remove('show-ui'), 2200);
}
addEventListener('pointermove', e => { if (e.pointerType === 'mouse') pokeUI(); });
pokeUI();

let torch = false;
for (const b of document.querySelectorAll('#toolbar button')) {
  b.addEventListener('pointerdown', e => e.stopPropagation());
  b.addEventListener('click', async () => {
    stage.focus({ preventScroll: true });
    if (b.dataset.key) send({ t: 'key', k: b.dataset.key });
    else if (b.dataset.cmd === 'rotate' && meta.flex) {
      // a flex display turns by giving the window the other orientation: the app relayouts to landscape/portrait
      const bar = innerHeight - areaH();
      send({ t: 'win', w: areaH(), h: areaW() + bar, iw: innerWidth, ih: innerHeight });
    }
    else if (b.dataset.cmd === 'torch') { torch = !torch; b.classList.toggle('on', torch); send({ t: 'cmd', c: 'torch', on: torch }); }
    else if (b.dataset.cmd) send({ t: 'cmd', c: b.dataset.cmd });
    else if (b.dataset.act === 'zoom') {
      magnifier = !magnifier; b.classList.toggle('on', magnifier);
      document.body.classList.toggle('magnifier', magnifier);
      if (!magnifier) resetView();
    } else if (b.dataset.act === 'stats') toggleStats();
    else if (b.dataset.act === 'shot') screenshot();
    else if (b.dataset.act === 'pin') setPinned(!pinned);
  });
}

// Always on top (the host remembers it per window)
let pinned = P.get('pinned') === '1';
function setPinned(on) {
  pinned = on;
  const b = $('[data-act=pin]');
  b.classList.toggle('on', on);
  b.title = on ? 'Kept on top (click to release)' : 'Keep on top';
  send({ t: 'pin', on });
}
$('[data-act=pin]').classList.toggle('on', pinned);
if (pinned) $('[data-act=pin]').title = 'Kept on top (click to release)';

async function screenshot() {
  try {
    const blob = await new Promise(r => canvas.toBlob(r, 'image/png'));
    await navigator.clipboard.write([new ClipboardItem({ 'image/png': blob })]);
    toast('Screenshot copied');
  } catch (e) { toast('Could not copy screenshot'); console.warn(e); }
}

// ------------------------------------------------------------------ stats
let statsOn = false, lastStats = performance.now(), lastLine = '';
function toggleStats() { statsOn = !statsOn; $('#stats').hidden = !statsOn; $('[data-act=stats]').classList.toggle('on', statsOn); }
setInterval(() => {
  const now = performance.now(), dt = (now - lastStats) / 1000;
  lastStats = now;
  const lat = st.lat.length ? st.lat.reduce((a, b) => a + b, 0) / st.lat.length : 0;
  const maxLat = st.lat.length ? Math.max(...st.lat) : 0;
  lastLine = `${vw}×${vh} ${codecStr || meta.codec || ''}  ${meta.display || ''}\n` +
    `${(st.frames / dt).toFixed(0)} fps  ${(st.bytes * 8 / dt / 1e6).toFixed(1)} Mbit/s\n` +
    `PC pipeline ${lat.toFixed(1)} ms (max ${maxLat.toFixed(0)})  queue ${decoder?.decodeQueueSize ?? '-'}\n` +
    `smoothing ${pacer.delay.toFixed(0)} ms  skipped ${st.skipped}\n` +
    `dropped ${st.dropped}  resyncs ${st.resets}  decoder errors ${st.decErr}`;
  window.__screen.last = { fps: st.frames / dt, mbps: st.bytes * 8 / dt / 1e6, lat, maxLat, vw, vh, gotFrame, dropped: st.dropped, resets: st.resets, q: decoder?.decodeQueueSize, delay: pacer.delay, skipped: st.skipped };
  st.frames = 0; st.bytes = 0; st.lat = [];
  if (statsOn) $('#stats').textContent = lastLine;
}, 1000);

// ------------------------------------------------------------------ test hooks (used by the e2e script)
let probe = null;
function sampleLuma() {
  const c = sampleLuma.c || (sampleLuma.c = new OffscreenCanvas(24, 48));
  const x = c.getContext('2d', { willReadFrequently: true });
  x.drawImage(canvas, 0, 0, 24, 48);
  return x.getImageData(0, 0, 24, 48).data;
}
window.__screen = {
  send,
  // send a message, resolve with ms until the rendered picture changes noticeably (tap-to-photon estimate)
  probe(msg, threshold = 6, timeout = 3000) {
    return new Promise(resolve => {
      const base = sampleLuma(), t0 = performance.now();
      probe = {
        check() {
          const cur = sampleLuma(); let d = 0;
          for (let i = 0; i < cur.length; i += 4) d += Math.abs(cur[i] - base[i]) + Math.abs(cur[i + 1] - base[i + 1]);
          d /= cur.length / 2;
          if (d > threshold) { probe = null; resolve({ ms: performance.now() - t0, diff: d }); }
          else if (performance.now() - t0 > timeout) { probe = null; resolve({ ms: -1, diff: d }); }
        },
      };
      for (const m of [].concat(msg)) send(m);
    });
  },
};

if (!('VideoDecoder' in window)) setStatus('This WebView has no WebCodecs support', true);
else connect();
