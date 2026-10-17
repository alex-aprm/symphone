# Symphone — status and roadmap

Every request so far, what was built, how far it is verified, and what is planned next. Updated for **2.7.1** (Windows app; phone app 2.7.0, versionCode 11).
(Windows app + phone app versionCode 9).

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
