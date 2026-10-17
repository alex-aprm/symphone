# PhoneSync virtual camera (Media Foundation)

Exposes the phone camera as a real Windows 11 camera ("Symphone Camera") using
`MFCreateVirtualCamera` (Windows 11 22000+). Visible to every Media Foundation and DirectShow app
(Camera app, Teams, Zoom, Chrome/Edge, OBS). Windows appends " (Windows Virtual Camera)" to the
name it shows; that suffix comes from the OS and can't be removed.

Design adapted from [smourier/VCamSample](https://github.com/smourier/VCamSample) (MIT), rewritten
without WIL or C++/WinRT. It produces CPU-only NV12 samples and takes its frames from shared memory.

## Pieces

| Path | What |
|---|---|
| `src/` | `PhoneSyncVCam.dll`: in-proc COM media source, CLSID `{4A97DD59-7288-40DC-A638-997758C35BD1}`. It implements `IMFActivate`, `IMFMediaSourceEx`, `IMFGetService`, `IKsControl` and `IMFSampleAllocatorControl`, and has one `IMFMediaStream2` stream. That stream offers NV12 1920x1080@30 and NV12 1280x720@30. |
| `host/PhoneSyncCam.cpp` | `PhoneSyncCam.exe` with the `install`, `uninstall`, `run`, `status`, `grab` and `selftest` commands. |
| `build.ps1` | Builds Release x64 into `bin\` (VS 2022 Build Tools, cl.exe). |
| `install.ps1` | Elevates itself. It copies `bin` to `C:\Program Files\PhoneSync\vcam\`, registers the COM DLL in HKLM and creates the System-lifetime camera. `-Uninstall` removes it all. |
| `test_pattern.py` | Pushes moving colour bars at 30 fps through `pc/phonesync/vcam.py`. |
| `proof/` | Frames captured through the Frame Server: MF source reader, OpenCV MSMF and OpenCV DirectShow. |

## Install / uninstall

```powershell
powershell -ExecutionPolicy Bypass -File C:\Repos\sync\vcam\build.ps1
powershell -ExecutionPolicy Bypass -File C:\Repos\sync\vcam\install.ps1                       # default name
powershell -ExecutionPolicy Bypass -File C:\Repos\sync\vcam\install.ps1 -Name "My Phone Cam"
powershell -ExecutionPolicy Bypass -File C:\Repos\sync\vcam\install.ps1 -Uninstall            # same -Name as installed
```

You can also call the exe directly (admin is needed for install and uninstall):

```
"C:\Program Files\PhoneSync\vcam\PhoneSyncCam.exe" install   --name "Symphone Camera"
"C:\Program Files\PhoneSync\vcam\PhoneSyncCam.exe" uninstall --name "Symphone Camera"
PhoneSyncCam.exe run --name "X" [--seconds N]    # session camera, no admin (COM source must already be registered)
PhoneSyncCam.exe status                          # COM registration, MF capture devices, shared-memory header
PhoneSyncCam.exe grab [--name substr] [--size 1280x720] [--frames N] [--out f.bmp]   # read frames via MF
```

The camera is created with `MFVirtualCameraLifetime_System` and `MFVirtualCameraAccess_AllUsers`,
so it persists across reboots. Removing it requires the same name that was used to install it.

## Shared-memory transport

* File mapping `Global\PhoneSyncVCam`, size 64 + 12,441,600 bytes.
* Auto-reset event `Global\PhoneSyncVCamFrame`. The producer signals it after each frame.
* Standard users can't create `Global\` objects. The **DLL creates both** inside the Frame Server
  service (LocalService) with the DACL `D:(A;;GA;;;AU)(A;;GA;;;SY)(A;;GA;;;LS)(A;;GA;;;NS)(A;;GA;;;BA)`.
  The producer only opens them. If the mapping already exists, the DLL opens it instead.
  The mapping is created when the Frame Server first instantiates the source, which happens right
  after install and whenever an app enumerates or opens the camera. It lives as long as a handle is
  open: the Frame Server's or the producer's.

Header (little-endian, packed, 64 bytes). Frame data starts at offset 64:

| Off | Type | Field | Written by |
|---|---|---|---|
| 0 | u32 | magic `'PSVC'` = 0x43565350 | DLL |
| 4 | u32 | version = 1 | DLL |
| 8 | u32 | width of the frame in the data area | producer |
| 12 | u32 | height | producer |
| 16 | u32 | format (1 = NV12) | producer |
| 20 | u64 | frame_counter, incremented **after** the frame is fully written | producer |
| 28 | u64 | timestamp_100ns (FILETIME UTC) | producer |
| 36 | u32 | consumer_active (1 while a stream is running) | DLL |
| 40 | u64 | consumer_heartbeat (FILETIME UTC, refreshed every 0.5 s while active) | DLL |
| 48 | u32 | req_width (current media type) | DLL |
| 52 | u32 | req_height | DLL |
| 56 | u32 | req_fps (30) | DLL |
| 60 | u32 | capacity = 12,441,600 | DLL |
| 64 | … | NV12 frame: Y plane (width×height), then interleaved UV (width×height/2), stride = width | producer |

Rules:

* The DLL shows a frame only if `format == 1` and `width/height == req_width/req_height`.
  The producer must scale to `requested_size` itself.
* If `frame_counter` hasn't changed for more than 1 s, or the size doesn't match, the DLL outputs
  a placeholder: a dark-gray frame with a centered camera glyph. Apps never freeze.
* Treat `consumer_active` as true only when `consumer_heartbeat` is less than about 3 s old.
  `vcam.py` does this. If the Frame Server dies, the flag can be left at 1.
* Output is paced at about 30 fps. A newly signalled frame is delivered early, as long as at least
  60% of a frame interval has passed.

Python side: `pc/phonesync/vcam.py` → `VirtualCamera().available`, `.consumer_active`,
`.requested_size`, `.write_nv12(data, w, h)` and `.close()`.

## Debugging

The DLL logs to `C:\ProgramData\PhoneSync\vcam.log` from every process that loads it. Each line
carries the PID and exe name, so the Frame Server shows up as `svchost.exe`. The log is truncated
at 8 MB. To replace the DLL while the Frame Server holds it, run `install.ps1` again: it stops
FrameServer and FrameServerMonitor if the copy fails.
