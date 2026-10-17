# Symphone protocol v2 (multi-phone / multi-PC)

Identity
- Phone: `Hub.deviceId` (12 hex), name = device name.
- PC: `pc_id` (12 hex, PC settings), `pc_name` (Windows computer name, editable).

## Phone side (HTTP :47900, existing server)

Auth: one token **per paired PC**. A request is authenticated when its token (X-Token / Bearer / Basic / `/t/<token>/`)
matches any paired PC; the server then knows the requesting PC (`req.pc`). The legacy global token keeps working and
maps to a PC entry with id `"legacy"` until that PC re-registers (see `POST /api/pc`).

Phone keeps `pcs`: `[{id, name, token, master:boolean, lastSeen, lastIp, wfd:{ssid,pass}|null}]` (SharedPreferences JSON).
Exactly one PC is master (the first paired one by default; switchable in the phone app or via `POST /api/pc/master`).

Endpoints (new/changed):
- `GET /api/ping` (no auth): + `"v":2`, `"ble":bool`.
- `POST /api/pair` (no auth) `{"code":"123456","pc":{"id","name"}}` → `{"token","id","name"}`. 6-digit code shown in the
  phone app (unchanged rate limits). Creates/updates the PC entry.
- `GET /api/info`: + `"you": <requesting pc id>`, `"master": <master pc id>`,
  `"pcs":[{"id","name","online","master"}]`, `"locked":bool`, `"screenOn":bool` (already there or added).
- `POST /api/pc` `{"id","name"}`: the requesting PC registers/renames itself (legacy entry gets its real id).
- `POST /api/pc/master` `{"id"}` (default: requester): make that PC master. Emits `pcs` event to everybody.
- `DELETE /api/pc`: the requester unpairs itself.
- `POST /api/pc/wfd` `{"ssid","pass"}`: the PC's Wi-Fi Direct legacy network (PC hosts it; phone joins it with
  WifiNetworkSpecifier when no LAN path to that PC exists).
- `GET /api/events` (SSE): the subscriber is tagged with the requesting PC. New events: `pcs` `{master, pcs:[...]}`,
  `clip` `{text}` (phone clipboard changed; only to the master PC). `share` / `share_text` go to the share's target PC.
- `GET /api/shares`: only shares targeted at the requester (`share.pc == requester`, or no explicit target and the
  requester is master, or the master has no live SSE subscriber).
- `POST /api/clipboard` `{"text"}`: sets the phone clipboard (remember it so it is not echoed back as `clip`).
- `POST /api/clip/shell` `{"text"}`: from the shell clipboard helper (see below); emits `clip` unless it equals the
  last text set from a PC.
- `POST /api/notif/act` `{key, op, index, text, display?}`: with `display` (int) the content/action PendingIntent is
  sent with `ActivityOptions.setLaunchDisplayId(display)` (fallback: without it).
- `POST /api/launch` `{pkg, display?}` same.

Shell clipboard helper: Android 10+ blocks background clipboard reads for apps, but adb shell can read it. The APK
contains `dev.phonesync.ShellClip` with a `main()`; the PC starts it over adb:
`CLASSPATH=<apk path> app_process / dev.phonesync.ShellClip <token> &` (nohup). It polls the clipboard as
`com.android.shell` every 500 ms (reflection on `IClipboard`, see scrcpy's ClipboardManager wrapper) and POSTs
changes to `http://127.0.0.1:47900/api/clip/shell` with `X-Token`.

Arrival: on every Wi-Fi connect (ConnectivityManager NetworkCallback) and every 60 s while on Wi-Fi the phone
- sends its UDP beacon `PHONESYNC!{json}` to broadcast **and** unicast to every paired PC's `lastIp:47800`,
- `POST http://<pc lastIp>:47801/peer/hello` `{"id","ip","port","ts","sig"}` where
  `sig = hex(HMAC-SHA256(key=token of that PC, msg = id + "|" + ip + "|" + ts))`.

### PC keyboard (invisible IME) and wallpaper (app 2.2, versionCode 4)

`dev.phonesync/.PcKeyboard` is an input method that never draws anything. The PC enables/selects it over adb
(`ime enable dev.phonesync/.PcKeyboard; ime set dev.phonesync/.PcKeyboard`) while its app windows are in use and
types through it, so any script (Cyrillic etc.) works and no soft keyboard appears on the phone.

- `POST /api/ime/lease` `{pkgs:[pkg...], any:bool, prev:"<user IME id>"|null, ttl:<s, default 20>}` → `{ok, current,
  ours, editor, bound}`. While the lease is valid the IME serves editors of apps in `pkgs` (or any app if `any`).
  An editor of another app, an expired lease, or a lease update that drops the focused app → it switches straight back
  to `prev` (else the previous/any other enabled IME). Renew every ~8 s.
- `POST /api/ime/release` → `{ok:true}`; clears the lease and switches back if the IME is current.
- `POST /api/ime/input` — one of `{text}` / `{paste}` (commitText), `{key:<keycode>, meta:<metaState>,
  action:"tap"|"down"|"up", repeat}` (sendKeyEvent). Runs synchronously on the main thread (≤500 ms), so request
  order is input order. `{ok:true}` or `{ok:false, reason:"not-current"|"no-editor"|...}`; on `false` the PC falls
  back to scrcpy key injection (also used for windows without a text field, which report `no-editor`).
- `GET /api/ime/status` → same shape as the lease response.
- `GET /api/wallpaper?w=<px, default 480>` → `image/jpeg` of the home-screen wallpaper scaled to width `w` (aspect
  kept — may be wider than the screen; crop to cover), header `X-Wallpaper-Id` (content hash); 404 `{error}` if not
  readable. Cached per width, invalidated on wallpaper change.

## PC side (LAN, HTTP :47801 — same server as the local UI; only `/peer/*` is reachable from the LAN)
- `POST /peer/pair` `{"s": secret, "phone":{"id","name","model","token","ip","port"}}` → `{"ok":true,"pc":{"id","name"}}`
  or 403. `s` is the secret from the QR code currently shown by the PC (valid ~10 min, single use).
- `POST /peer/hello` (as above) → `{"ok":true}`; PC verifies sig and immediately reconnects.

## QR code (shown by the PC in Settings → Add phone)
`phonesync://pair?v=2&pc=<pc id>&n=<url-encoded pc name>&s=<secret 32 hex>&a=<ip1>,<ip2>&p=47801[&b=1][&w=<ssid>&k=<pass>]`
Phone flow after scanning (in-app scanner using ZXing core, or the system camera opening the `phonesync://` link):
1. Generate a token for this PC, `POST /peer/pair` to each address in `a` (3 s timeout each); first success wins.
2. If `w`/`k` present and nothing answered: join that Wi-Fi Direct network (WifiNetworkSpecifier) and retry on
   `192.168.137.1`.
3. If `b=1` and still nothing: BLE pairing (below).
Save the PC (master if it is the first/only PC).

## BLE
Service UUID `5e7c0001-9b0d-4c55-9e6a-50a0c3e7d6a1`. The phone advertises (low power, connectable) while its service
runs: service data on that UUID = 6 bytes `SHA-256(deviceId)[0..6]` + 4 bytes IPv4 of its Wi-Fi (zeros if none).
The PC scans (WinRT watcher) → known phone nearby with a new IP → update `last_ip` and connect.
GATT (phone = server):
- `5e7c0002-…` READ: `{"id","name","ip","port"}` (JSON, nothing secret).
- `5e7c0003-…` WRITE (pairing request from the PC, while the phone has a pending QR pairing):
  `nonce(12) | AES-GCM(key=SHA-256(secret), plaintext = {"pc","n"})`.
- `5e7c0004-…` READ (after a successful write): `nonce(12) | AES-GCM(key=SHA-256(secret), {"id","name","model","token","ip","port"})`.

## Phone app 2.5 additions

- `GET /api/cameras` → `{cameras: [{id, facing: back|front|external, label, fov, sensorOrientation}]}` — cameras an
  app can open (ids as scrcpy uses them); `label` comes from the lens' field of view (main / ultra-wide / telephoto).
- `GET /api/camera/orientation?id=<camera id>` → `{id, device, sensor, rotation}` — clockwise rotation that makes that
  camera's frames upright for the phone's current physical orientation (Camera2 formula). The orientation sensor
  only runs while this is polled (stops 15 s after the last call).
- `POST /api/pc/wfd {ssid, pass, prefer}` — `prefer: true` asks the phone to keep the PC's Wi‑Fi Direct network
  joined even while the LAN works, if it can stay on its own Wi‑Fi at the same time.
- `/api/info` adds `wfdIp` (the phone's address on the PC's direct network, or "") and `staConcurrency`
  (local-only STA + STA concurrency: the phone can use the direct link without leaving its Wi‑Fi).

## Debug-free mode (no USB / wireless debugging)

Everything below works with permissions the user grants in the phone's Settings (the onboarding checklist asks for
them). With adb available the PC keeps using it (it adds app windows on hidden displays while the phone is locked).

### Capabilities
`GET /api/caps` → `{accessibility, cast, camera, mic, writeSettings, dnd, overlay: bool, streamPort: 47904}` —
what the phone can do without adb right now (`accessibility` = the Symphone control service is running; `overlay`
= "Display over other apps", which lets the phone start camera/mic streams and casting consent from the background).

### Media stream — TCP port 47904
One connection per stream. The PC sends one JSON line:
`{"token": "...", "kind": "screen"|"camera"|"audio", "max_size": 1920, "fps": 60, "bitrate": 8000000,
  "camera": "<id>"|"", "facing": "back"|"front", "audio": "playback"|"mic"}`
The phone answers one JSON line: `{"ok": true, "width": W, "height": H, "codec": "h264"|"pcm", "device": "<model>"}`
(screen also has `"control": bool`, whether input will work) or `{"ok": false, "error": "...", "needs":
"consent"|"permission"}` (`needs` absent for a bad token or other errors). The screen is never refused for a
missing accessibility service; it is just view-only.
- `kind: screen` asks for Android's screen-casting consent on the phone (every session on Android 14+; the answer
  can take up to 60 s). `needs: "permission"` when the consent can't be shown (app in the background without
  "Display over other apps").
- `kind: camera` needs the Camera permission; 16:9 size ≤ `max_size`, 30 fps. `kind: audio` needs the Microphone
  permission; `audio: "playback"` (other apps' sound; apps can opt out, calls are never captured) also asks for
  casting consent like `screen`.

Then phone → PC, repeated: `u8 type` +
- `0x01` video: `u8 flags (1 config, 2 keyframe) | u64 BE pts µs | u32 BE length | Annex B payload` — the first
  packet is the SPS/PPS config on its own (as with scrcpy)
- `0x02` audio: `u64 BE pts µs | u32 BE length | PCM s16le 48 kHz stereo` (20 ms chunks)
- `0x10` event: `u32 BE length | UTF-8 JSON` — `{"type":"size","width","height"}` (rotation; a new config and
  keyframe follow), `{"type":"ended","reason"}` (always sent last when the phone ends the stream: casting stopped
  from the phone, camera taken by another app, …)

PC → phone on the same socket: newline-delimited JSON input (screen streams; needs the accessibility service):
`{"t":"touch","a":0|1|2,"id":n,"x":0..1,"y":0..1}` (MotionEvent down/up/move, normalized to the phone's display;
several ids = multi-finger gesture),
`{"t":"scroll","x","y","h","v"}` (wheel notches),
`{"t":"key","code":<Android KeyEvent keycode>,"a":0|1 (down/up),"meta","repeat"}` — acted on at key down (up is
ignored); BACK/ESCAPE, HOME, APP_SWITCH, NOTIFICATION, POWER (locks) and volume keys map to global actions; editing
keys (DEL, FORWARD_DEL, ENTER, TAB, arrows with Shift, MOVE_HOME/END, SPACE, Ctrl+A/C/X/V) act on the focused field,
`{"t":"text","s":"…"}` (inserted at the cursor of the focused field), `{"t":"global","action":"notifications"|"quick_settings"|"back"|"home"|"recents"|"lock"}`,
`{"t":"launch","pkg":"…"}` (opens an app on the phone's screen), `{"t":"torch","on"}`, `{"t":"zoom","in"}` (camera),
`{"t":"reset"}` (new keyframe). `{"t":"rotate"}` is accepted and ignored (apps can't rotate the screen).

### Control without adb
- `POST /api/input {…}` — the same input messages outside a stream → `{ok}` or `{ok:false, needs:"accessibility"}`.
- `POST /api/lock` — locks the phone (accessibility).
- `POST /api/unlock {nonce, ct}` — PIN encrypted with AES-256-GCM, key = SHA-256(token), nonce 12 bytes, both hex
  (`ct` includes the tag), plaintext `{"pin":"…"}` (digits only). The phone wakes the screen, opens the PIN pad
  (clicks the lock icon, else swipes up — Pixels ignore synthetic swipes on the lock screen), taps the digits and
  Enter once and never retries → `{ok:true, message:"unlocked"|"already unlocked"}` or `{ok:false, error}`
  (e.g. keypad not found, still locked). If the keypad closes mid-PIN it stops before Enter. Pattern and
  alphanumeric passwords are not supported.
- `GET /api/controls` → `{toggles: {<id>: {label, on, settable}}, brightness: 0..255, volumes: {media|ring|alarm|call:
  {value, min, max}}, torch: bool, adb: false}` — toggle ids `wifi`, `bluetooth`, `data`, `airplane`, `dnd`,
  `location`, `rotate`, `saver`, `dark`, `nfc`, `autobright`; `settable` = switched right on the phone.
- `POST /api/controls` with one of `{toggle: <id>|"torch", on}`, `{brightness: 1..255}`, `{volume: <stream>, value}`
  → `{ok}`. `rotate`, `autobright` and `brightness` need "Change system settings", `dnd` needs "Do Not Disturb
  access" (`{ok:false, needs:"write_settings"|"dnd", error}` otherwise). Apps can't switch the others since Android
  10: the phone opens its own panel and returns `{ok:true, opened:true}`.
- Clipboard phone → PC: Quick Settings tile *Send clipboard to PC* and the same action on the app's notification
  (Android blocks background clipboard reads); sent as the usual `clip` event to the main PC.

## Phone app 2.6: setup checklist

- `GET /api/setup` (X-Token) → `{items: [{id, title, why, ok, required, hint}], done, requiredOk, installer}` — the
  phone's first-run checklist, live. `ok` is `true` / `false`, or `null` when Android can't tell (vendor
  auto-launch switches). Item ids: `paired`, `notifications` (13+), `notif_access`, `all_files`, `battery`,
  `control` (accessibility service, 2.7), `overlay`, `camera`, `mic` (2.7), `write_settings` (2.7), `dnd` (2.7),
  `calls`, `nearby` (12+ with BLE), `autostart` (known vendors only), `wireless_debugging` (optional extra since
  2.7: only for app windows while the phone is locked).
  Required: `paired`, `notif_access`, `all_files`, `battery`. `hint` carries the steps when Android needs manual
  action (e.g. "Allow restricted settings" for notification access on apps installed from an APK file, Android
  13+). `installer` is the installing package (`com.android.vending`, a package installer for an APK file, or null
  for adb).
- `phonesync://pair?...` links open the app from any QR scanner/camera (PairActivity, asks to confirm); after a
  first pairing it continues to the checklist.
