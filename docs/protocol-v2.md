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
