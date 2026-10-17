"""Producer side of the PhoneSync Media Foundation virtual camera.

The camera DLL (vcam/PhoneSyncVCam.dll, loaded by the Windows Frame Server) creates a
shared-memory section ``Global\\PhoneSyncVCam`` and an auto-reset event
``Global\\PhoneSyncVCamFrame``. This module opens them and publishes NV12 frames.

Layout (little-endian, packed, 64-byte header, frame data at offset 64)::

    0  u32 magic 'PSVC' (0x43565350)   4  u32 version (1)
    8  u32 width                      12  u32 height          16 u32 format (1 = NV12)
    20 u64 frame_counter              28  u64 timestamp_100ns (FILETIME UTC)
    36 u32 consumer_active            40  u64 consumer_heartbeat (FILETIME UTC)
    48 u32 req_width                  52  u32 req_height      56 u32 req_fps   60 u32 capacity
    64 .. frame data (capacity 3840*2160*3/2 = 12,441,600 bytes)

Only the stdlib is required (ctypes). numpy arrays are accepted by ``write_nv12`` via the
buffer protocol.
"""
from __future__ import annotations

import ctypes
import struct
import sys
import time

__all__ = ["VirtualCamera", "MAPPING_NAME", "EVENT_NAME"]

MAPPING_NAME = "Global\\PhoneSyncVCam"
EVENT_NAME = "Global\\PhoneSyncVCamFrame"
MAGIC = 0x43565350
VERSION = 1
FORMAT_NV12 = 1
HEADER_SIZE = 64
FRAME_CAPACITY = 3840 * 2160 * 3 // 2
MAPPING_SIZE = HEADER_SIZE + FRAME_CAPACITY

# header offsets
_OFF_MAGIC, _OFF_VERSION, _OFF_W, _OFF_H, _OFF_FMT = 0, 4, 8, 12, 16
_OFF_COUNTER, _OFF_TS, _OFF_ACTIVE, _OFF_HEARTBEAT = 20, 28, 36, 40
_OFF_REQ_W, _OFF_REQ_H, _OFF_REQ_FPS, _OFF_CAPACITY = 48, 52, 56, 60

_FILE_MAP_WRITE = 0x0002
_FILE_MAP_READ = 0x0004
_EVENT_MODIFY_STATE = 0x0002
_SYNCHRONIZE = 0x00100000
_HEARTBEAT_TIMEOUT_S = 3.0
_RETRY_INTERVAL_S = 1.0

_k32 = None                 # the camera is a Windows (Media Foundation) component: never available elsewhere
if sys.platform == "win32":
    from ctypes import wintypes
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.OpenFileMappingW.restype = wintypes.HANDLE
    _k32.OpenFileMappingW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    _k32.MapViewOfFile.restype = ctypes.c_void_p
    _k32.MapViewOfFile.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_size_t]
    _k32.UnmapViewOfFile.restype = wintypes.BOOL
    _k32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
    _k32.OpenEventW.restype = wintypes.HANDLE
    _k32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    _k32.SetEvent.restype = wintypes.BOOL
    _k32.SetEvent.argtypes = [wintypes.HANDLE]
    _k32.CloseHandle.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]


def _filetime_now() -> int:
    """Current UTC time as FILETIME (100ns since 1601-01-01)."""
    return int(time.time() * 10_000_000) + 116444736000000000


class VirtualCamera:
    """Publishes NV12 frames to the PhoneSync virtual camera.

    ``available`` is False until the camera DLL has created the shared memory (i.e. the
    camera is installed and the Frame Server loaded it at least once); every property
    access retries opening lazily (at most once per second).
    """

    def __init__(self) -> None:
        self._map = None
        self._event = None
        self._base = 0
        self._last_try = 0.0
        self._counter = 0
        self._open()

    # -- connection -------------------------------------------------------
    def _open(self) -> bool:
        if self._base:
            return True
        if _k32 is None:
            return False
        now = time.monotonic()
        if now - self._last_try < _RETRY_INTERVAL_S and self._last_try:
            return False
        self._last_try = now
        h = _k32.OpenFileMappingW(_FILE_MAP_READ | _FILE_MAP_WRITE, False, MAPPING_NAME)
        if not h:
            return False
        base = _k32.MapViewOfFile(h, _FILE_MAP_READ | _FILE_MAP_WRITE, 0, 0, MAPPING_SIZE)
        if not base:
            _k32.CloseHandle(h)
            return False
        if self._u32(_OFF_MAGIC, base) != MAGIC:
            _k32.UnmapViewOfFile(base)
            _k32.CloseHandle(h)
            return False
        self._map, self._base = h, base
        self._event = _k32.OpenEventW(_EVENT_MODIFY_STATE | _SYNCHRONIZE, False, EVENT_NAME) or None
        self._counter = self._u64(_OFF_COUNTER)
        return True

    @property
    def available(self) -> bool:
        return self._open()

    # -- raw header access ------------------------------------------------
    def _u32(self, off: int, base: int | None = None) -> int:
        return ctypes.c_uint32.from_address((base or self._base) + off).value

    def _u64(self, off: int) -> int:
        # unaligned u64 (packed header) -> read via bytes
        return struct.unpack_from("<Q", ctypes.string_at(self._base + off, 8))[0]

    def _set_u32(self, off: int, v: int) -> None:
        ctypes.c_uint32.from_address(self._base + off).value = v

    def _set_u64(self, off: int, v: int) -> None:
        ctypes.memmove(self._base + off, struct.pack("<Q", v & 0xFFFFFFFFFFFFFFFF), 8)

    # -- consumer state ---------------------------------------------------
    @property
    def consumer_active(self) -> bool:
        """True while an app is reading the camera (flag set + heartbeat fresh)."""
        if not self._open():
            return False
        if not self._u32(_OFF_ACTIVE):
            return False
        hb = self._u64(_OFF_HEARTBEAT)
        return (_filetime_now() - hb) / 10_000_000 < _HEARTBEAT_TIMEOUT_S

    @property
    def requested_size(self) -> tuple[int, int]:
        """(width, height) the camera's current media type expects; default 1920x1080."""
        if not self._open():
            return (1920, 1080)
        w, h = self._u32(_OFF_REQ_W), self._u32(_OFF_REQ_H)
        return (w, h) if w and h else (1920, 1080)

    @property
    def requested_fps(self) -> int:
        if not self._open():
            return 30
        return self._u32(_OFF_REQ_FPS) or 30

    @property
    def frame_counter(self) -> int:
        return self._u64(_OFF_COUNTER) if self._open() else 0

    # -- producer ---------------------------------------------------------
    def write_nv12(self, data, width: int, height: int) -> bool:
        """Copy one NV12 frame (width*height*3/2 bytes) into shared memory and signal it.

        The DLL only shows frames whose size equals ``requested_size``; other sizes
        are ignored (the placeholder is shown). Returns False if the camera isn't available.
        """
        if not self._open():
            return False
        size = width * height * 3 // 2
        if width <= 0 or height <= 0 or width % 2 or height % 2:
            raise ValueError("NV12 needs positive, even width/height")
        if size > FRAME_CAPACITY:
            raise ValueError(f"frame {width}x{height} exceeds capacity")
        mv = memoryview(data).cast("B")
        if mv.nbytes < size:
            raise ValueError(f"need {size} bytes, got {mv.nbytes}")
        if isinstance(data, bytes):
            ctypes.memmove(self._base + HEADER_SIZE, data, size)
        elif mv.readonly:
            ctypes.memmove(self._base + HEADER_SIZE, mv[:size].tobytes(), size)
        else:
            buf = (ctypes.c_char * mv.nbytes).from_buffer(mv)
            ctypes.memmove(self._base + HEADER_SIZE, buf, size)
        self._set_u32(_OFF_W, width)
        self._set_u32(_OFF_H, height)
        self._set_u32(_OFF_FMT, FORMAT_NV12)
        self._set_u64(_OFF_TS, _filetime_now())
        self._counter = max(self._counter, self._u64(_OFF_COUNTER)) + 1
        self._set_u64(_OFF_COUNTER, self._counter)  # published last
        if self._event:
            _k32.SetEvent(self._event)
        return True

    def close(self) -> None:
        if self._base:
            _k32.UnmapViewOfFile(self._base)
            self._base = 0
        if self._map:
            _k32.CloseHandle(self._map)
            self._map = None
        if self._event:
            _k32.CloseHandle(self._event)
            self._event = None

    def __enter__(self) -> "VirtualCamera":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __del__(self) -> None:  # pragma: no cover
        try:
            self.close()
        except Exception:
            pass
