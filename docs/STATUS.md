# Symphone — status and roadmap

Every request so far, what was built, how far it is verified, and what is planned next. Updated for **2.8.8** (Windows app; phone app 2.8.6, versionCode 19).

Legend: ✅ done and verified on the real phone (nubia Z80 Ultra, Android 16) · ☑️ done, verified on the emulator or
partly · 🔬 done, not verified yet (needs a hands-on test) · ⛔ not possible as asked (why, and what was done instead).

## v2 feature list

| # | Request | Status | Notes |
|---|---|---|---|
| 1 | Several phones in the Windows app, several PCs in the phone app, switch the main PC | ✅ | Sidebar switcher; phone lists PCs with a *main PC*; "Make this PC its main PC" from the PC. |
| 1 | Pairing with QR scan over BLE, Wi‑Fi Direct and Wi‑Fi | 🔬 | QR + code + USB pairing built; code/USB verified, QR scan / BLE / Wi‑Fi Direct join only on the emulator side. |
| 2 | Auto connect by any means when the phone joins the Wi‑Fi | ✅ | UDP beacon + signed hello on every Wi‑Fi join, BLE presence, Wi‑Fi Direct fallback. |
| 3–4 | Sidebar and icons in Windows Settings style, colors from Windows | ✅ | Segoe Fluent icons, light/dark + accent from Windows, Mica (see below). |
| 5 | Sidebar: phone name, battery, connection + reconnect, one lock button, Status/Photos/Files/Apps/Settings/Side display | ✅ | Status is the default page. |
| 6 | Drop the touchpad | ✅ | Removed from both apps. |
| 7 | All notifications clickable, open the app like on the phone | ✅ | Toast clicks go through the `phonesync:` protocol (also from Action Center after a restart); the app opens in a PC window at the notification's target; a task already open on the phone's screen is pulled into the window. |
| 8 | Graceful hand-over when the app is used on the phone, blur, restore | ✅ | Patched scrcpy server *content gate*: frames stop the moment the app leaves its display (no launcher frame ever shows); last frame blurred; sharpens back when the app returns; a click on the blurred window pulls it back. |
| 9 | Native Windows transfer notifications, don't open lots of windows | ✅ | One progress toast per job, Open / Show in folder. |
| 10 | Send To item in Windows | ✅ | One entry per phone, with the phone's picture as icon. |
| 11 | Send to PC from the phone | 🔬 | Share target + one direct-share target per PC; not checked in the real share sheet. |
| 12 | Shared clipboard by default | ✅ | Both ways; the phone-side helper start bug was fixed; no "pasted" toasts for clips coming from the PC. |
| 13 | All settings on the Settings page | ✅ | |

## Later requests

| Request | Status | Notes |
|---|---|---|
| Touchpad two-finger scroll caused a click on lift | ✅ | Touch slop + momentum-tail filter. |
| PC keyboard in app windows, no phone keyboard | ✅ | Invisible *PhoneSync PC keyboard* input method; app windows use the "local" IME policy so it knows the field is on a PC display; any language (verified with Cyrillic). |
| Black stripes at the sides of app views | ✅ | Virtual displays follow the window size (flex display) at the phone's own density. |
| Tesla app layout broken | ✅ | Caused by a different display density; displays now use the phone's density. |
| Telegram notification click did nothing | ✅ | Task pulled from the phone's screen into the window; protocol activation. |
| "Some picture" when opening an app on the phone → blurred last frame | ✅ | Content gate (see #8). |
| Picture of the phone (model photo from the internet), change it | ✅ | Model photo from Wikimedia Commons (free license, credit shown), auto-cropped, phone's wallpaper put on its screen; *Change picture…* per phone. |
| Phone next to *This PC* in Explorer, not inside | 🔬 | Registered at the navigation pane root; please confirm visually. |
| App window toolbar below the picture | ✅ | Own strip under the picture. |
| Apps that forbid screenshots are black (AtGo) | ⛔ | Android blanks FLAG_SECURE windows everywhere but the phone's own screen; no non-root way around it. The window now says so and offers *Continue on the phone*. |
| Transparency like Windows, title bar same color as background | ✅ | Mica behind the whole window when *Transparency effects* are on; otherwise the title bar takes the background color. |
| Side display DPI: slider up to 600, default 300, checkbox | 🔬 | Text size 100–600 %, default 300 %, *Automatic* checkbox. Windows caps this monitor at 200 %, so larger sizes use lower driver modes (added to `vdd_settings.xml`, driver reloaded over its pipe; modes accepted by Windows). Not yet run end to end with the side display streaming. |
| Sound route switches don't remember their state | ✅ | Saved; a supervisor restores them after restarts / reconnects. |
| Badge counts notifications without a close button | ✅ | Only dismissable notifications and calls count. |
| Rename to Symphone | ✅ | Everything visible renamed; settings/caches migrate; internal ids kept (`dev.phonesync`, `phonesync`, `phonesync:`, camera names) so pairings and installs keep working. |
| Public GitHub repo, APK build and Windows releases on GitHub | ✅ | `alex-aprm/symphone`; `android.yml` on every push, `release.yml` on `v*` tags (patched scrcpy server, MSVC helpers, PyInstaller app, zip + APK). CI APK is signed with the same key as the installed app. |
| License: everyone may use the apps, code reuse needs permission | ✅ | Custom source-available `LICENSE` + `THIRD_PARTY_NOTICES.md`. Not legal advice: have a lawyer look at it if it matters commercially. |
| Wi‑Fi Direct as the preferred connection if possible | ⛔/☑️ | Built: phones that can keep their own Wi‑Fi while joined to a local-only network stay on the PC's direct network, ranked above the LAN. This nubia reports no such concurrency, so joining would drop its home Wi‑Fi — it stays a fallback there. |
| Screenshots with blurred personal info in the README | ✅ | `pc/tools/screenshots.py` (headless Edge, personal text blurred with CSS). |
| Webcam: camera pick, mirror, rotate, follow the phone's rotation | ✅ | Upright output verified with the phone turned; only two cameras are exposed by this phone to apps (back, front). |
| Buy me a coffee link | ✅ | `.github/FUNDING.yml` (Sponsor button) + README. |
| Icon: phone with a G (treble) clef | ✅ | `pc/tools/make_icon.py` → Windows `.ico`/`.png`, Android adaptive icon (incl. themed). Clef from Noto Music (OFL). |
| Always-on-top pin for app and phone windows | ✅ | Pin button in the window's toolbar; remembered per window (each app, the phone screen, the camera), so it reopens pinned. |

## Setup and debugging-free mode (2.6–2.7)

| Request | Status | Notes |
|---|---|---|
| Smooth installation: installer, permissions with explanations, checklist, QR pairing | ✅ | `Symphone-Setup.exe` (per user, no admin); PC setup wizard (get the app by QR / USB → pair by QR → the phone's checklist live → optional wireless debugging by QR → optional admin extras → done); phone onboarding asks what it can and opens the exact Settings screen otherwise, incl. Android 13+ "Allow restricted settings" for sideloaded apps. |
| Test from zero to a paired app | ✅ | Fresh install from the CI installer with empty data, wiped emulator: sideloaded APK → QR pairing → required permissions through the phone's own screens → casting, quick settings and lock from the PC, all without adb. |
| Remove the need for debugging | ✅ | Accessibility service (touch, keys, typing, lock, PIN unlock once), screen casting, the app's own camera / audio streams, quick settings via special permissions, "Send clipboard" tile. Wireless debugging is an optional extra for app windows while the phone is locked and call audio (Android allows hidden displays only to system/partner apps). |
| QR code cut in the wizard | ✅ | Fixed in 2.7.1 (QR SVGs scale). |

## 2.8.8

| Request | Status | Notes |
|---|---|---|
| Pin to front doesn't keep | ✅ | Not reproduced directly (pin, unpin, reopen and new windows all kept the flag), but two gaps closed: the pin button went over the window's stream socket, so a click while it reconnected was lost (now over HTTP); and pinned windows are checked every 2 s and put back on top if anything cleared always-on-top. Verified: always-on-top cleared from outside came back within 2 s. |

## 2.8.7

| Request | Status | Notes |
|---|---|---|
| Clicking a notification opens its app window in the background | ✅ | Windows only lets the app the user last interacted with activate a window, and a toast click is a click on Windows' own UI. Phone windows are now activated with fallbacks when the plain call is refused (shared input with the foreground thread, then a synthetic Alt tap). Verified: an app window opened by a background request came up over Chrome as the active window. |

## 2.8.6

| Request | Status | Notes |
|---|---|---|
| Telegram's placeholder typed into its message field | ✅ | Debug-free typing used to read the field and set the whole text back; Telegram (like Settings search) reports its placeholder as the field's text, so it ended up in the message. On Android 13+ the accessibility service now has its own input connection (flagInputMethodEditor): text is committed at the cursor like a keyboard does, never reading the field. Verified on the Settings search field, which reports its hint as text the same way: typed text replaces the hint. |
| No on-screen keyboard while typing from the PC | ✅ | Debug-free: while the PC shows the phone, the accessibility service keeps the soft keyboard hidden (back to normal when the window closes; verified: mode 1 while casting, 0 after). Over adb the phone-screen window now also uses the invisible PC keyboard while it has focus, as app windows already did (verified: switches to it on focus, back to Gboard on blur). |
| Ctrl shortcuts | ✅ | Debug-free keys go to the app as real key events (Ctrl+A, Ctrl+Z, Ctrl+Backspace, Ctrl+arrows… handled by the app; verified Ctrl+A and Ctrl+Backspace). Ctrl+C / Ctrl+X put the selection on the PC's clipboard (verified). Fixed on the way: that clipboard event was written on the main thread, which closed the stream. |

## 2.8.5

| Request | Status | Notes |
|---|---|---|
| Dismissing a notification on the PC dismisses it on the phone | ✅ | Closing the toast (Windows reports "user canceled") or clearing it in Action Center (seen there, then gone: Windows raises no event for that, so Symphone checks every 4 s) dismisses it on the phone. Clicked toasts, calls and notifications the phone doesn't let you clear are left alone. Verified: a test notification cleared from Action Center disappeared from the phone. |
| Toast says "Symphone" instead of the app | ✅ | Each phone app gets its own Windows toast sender (name + icon, registered when it first notifies), so the toast is headed by the app's name and Windows' notification settings list the apps separately. The uninstaller removes them. |

## 2.8.4 (2.8.3 was not released)

| Request | Status | Notes |
|---|---|---|
| Wi-Fi phone screen slow / laggy | ✅ | Measured on the nubia over Wi-Fi (debug-free casting, real viewer window). Causes: the phone encoded every composition of its 120 Hz panel (~110 fps, 8 Mbit/s spread thin), and the viewer, holding at most 4 frames while smoothing jitter by up to 60 ms, kept pushing out the oldest frame so nothing ever came due: ~9-21 fps on screen during scrolling. Fixes: encoder capped at the requested fps (max-fps-to-encoder), the viewer shows the oldest held frame instead of dropping it, smoothing capped at 30 ms. Result: steady 60 fps from the phone, ~46 fps average shown while scrolling (was 21), smoothing ~25 ms (was 36-60), 95% of frames within 14-22 ms of the best one (was 70 ms). |
| Compression | ✅ | H.265 on Wi-Fi when the PC decodes it in hardware (WebCodecs check): the phone app (debug-free) and scrcpy (adb over Wi-Fi) encode H.265, falling back to H.264 if the phone can't or the decoder fails. ~21% less data at the same frame rate. USB stays H.264. |
| H.265 picture cropped | ✅ | Windows' H.265 decoder put out 1280x720 frames (a crop) when WebCodecs wasn't given the coded size; the viewer now configures the decoder with the stream size. |
| Remove phone confirmation vanished | ✅ | The confirmation bar is now part of the phone list, so state updates no longer wipe it. |

## 2.8.0

| Request | Status | Notes |
|---|---|---|
| Unlock the phone with a nearby device | ❌ | Removed again (Extend Unlock integration dropped from both apps): opening Google's Extend Unlock screen from Symphone left a "Something came up. The application has cancelled the request to choose a device" dialog on the phone. Add the PC as a trusted device in the phone's own settings if wanted. |

## 2.7.5

| Request | Status | Notes |
|---|---|---|
| Side display doesn't work (without debugging) | ✅ | Its phone screen was only started over adb; the phone app now opens it itself (`POST /api/sidecar`, shows over the lock screen). Verified on the nubia: streaming over Wi‑Fi with adb hidden, and over USB with adb. |

## 2.7.4

| Request | Status | Notes |
|---|---|---|
| Phone / app windows on a locked phone without debugging | ✅ | Debugging-free mode can only show the phone's own screen: opening the window wakes and unlocks it with the saved PIN (typed once by Symphone control), and closing it locks the phone again. Verified on the nubia over Wi‑Fi with adb hidden: unlocked while open, live picture, locked and dozing after. |
| Back to the home screen after leaving an app | ✅ | Closing an app window (debugging-free) presses Home before locking, so the app isn't left on the phone's screen. Verified (launcher on top afterwards). |
| No "start recording?" dialog | ✅ | Verified on the nubia with phone app 2.7.3: `PROJECT_MEDIA` granted over USB, then casting starts with no dialog, locked or not. |

## 2.7.3

| Request | Status | Notes |
|---|---|---|
| Phone view without wireless debugging asks "start recording?" every time | ✅ | Whenever the PC has adb (setup, USB, debugging on once) it grants Symphone the `PROJECT_MEDIA` app-op: Android then allows casting without the dialog, also after reboots and with debugging off (verified: stream in ~3 s, no dialog). Without it, the Symphone control accessibility service confirms Symphone's own dialog (only that dialog, only right after our request; verified on Android 15). Casting asks for the whole screen directly (no "single app" choice). The PC also turns on that accessibility service (keeping others) and no longer switches wireless debugging back on. App windows on their own displays still need adb (shell-only virtual displays); without it an app opens on the phone's screen in the phone window. |

## 2.7.2

| Request | Status | Notes |
|---|---|---|
| Black screen over Wi‑Fi | ✅ | Root cause: a second copy of the app could start (the first was busy) and take over the local UI port, so viewers dropped. Now a single instance; also fixed a freeze when bringing the window forward or pinning (pywebview set TopMost cross-thread while holding the GIL). App windows keep their picture while the phone is locked. Smoother Wi‑Fi video (frame pacing, Wi‑Fi bitrate, no Wi‑Fi power save while streaming). |
| Unlock from the PC failed | ✅ | With app windows open, the PIN went to the focused virtual display; keys now always go to the phone's own screen (`input -d 0`). |
| Why not Wi‑Fi Direct | ⛔ | This phone can't stay on its Wi‑Fi and the PC's direct network at once (no STA+STA for local-only); Wi‑Fi Direct is used only when there is no LAN path. |
| App is a singleton on Windows | ✅ | Named mutex; a second launch brings the window to the front. |
| Copy in Photos (Ctrl+C, toolbar, right-click) | ✅ | Originals on the clipboard as files (Explorer, chats, mail) and, for one photo, as a picture (Paint, Word). |
| Demo GIF in the README | ✅ | `pc/tools/demo_gif.py`: tour + three phone apps at once, one operated from the PC; personal details blurred. |

## Next cycles

1. **Hands-on verification on the phone** (needs you holding it): QR scan pairing with the real camera, BLE pairing, Wi‑Fi Direct join, the accessibility unlock on the nubia lock screen,
   direct-share targets in the share sheet, *Continue on the phone* for protected apps, side display at 300 %+,
   Explorer navigation pane placement.
2. **Wi‑Fi Direct for phones without local-only concurrency**: have the phone host a Wi‑Fi P2P group and the PC join
   it through the Windows Wi‑Fi Direct API (keeps both on their home Wi‑Fi). Needs research on prompts per connect.
3. **Full internal rename** (`dev.symphone`, Python package, link scheme, camera) with an automatic re-pair /
   reinstall path.
4. **Windows packaging**: a real installer (MSIX or Inno Setup), code signing (SmartScreen), and app self-update from
   GitHub Releases (the phone app already updates from the PC).
5. **Content gate on non-nubia phones**: event-driven (task stack listener) instead of 30 ms polling.
6. **Release hygiene**: changelog per release, automated UI smoke tests in CI, README GIFs.
