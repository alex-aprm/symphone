// PhoneSync virtual camera - shared definitions (DLL + host).
#pragma once

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#include <stdint.h>

// {4A97DD59-7288-40DC-A638-997758C35BD1}
static const GUID CLSID_PhoneSyncVCam =
{ 0x4a97dd59, 0x7288, 0x40dc, { 0xa6, 0x38, 0x99, 0x77, 0x58, 0xc3, 0x5b, 0xd1 } };
#define PSVC_CLSID_STRING L"{4A97DD59-7288-40DC-A638-997758C35BD1}"

#define PSVC_MAPPING_NAME   L"Global\\PhoneSyncVCam"
#define PSVC_EVENT_NAME     L"Global\\PhoneSyncVCamFrame"
#define PSVC_SDDL           L"D:(A;;GA;;;AU)(A;;GA;;;SY)(A;;GA;;;LS)(A;;GA;;;NS)(A;;GA;;;BA)"

#define PSVC_MAGIC          0x43565350u   // 'PSVC' little-endian
#define PSVC_VERSION        1u
#define PSVC_FORMAT_NV12    1u
#define PSVC_HEADER_SIZE    64u
#define PSVC_MAX_WIDTH      3840u
#define PSVC_MAX_HEIGHT     2160u
#define PSVC_FRAME_CAPACITY (PSVC_MAX_WIDTH * PSVC_MAX_HEIGHT * 3u / 2u)   // 12,441,600
#define PSVC_MAPPING_SIZE   (PSVC_HEADER_SIZE + PSVC_FRAME_CAPACITY)

#pragma pack(push, 1)
struct PsvcHeader
{
    uint32_t magic;              //  0  'PSVC'
    uint32_t version;            //  4  1
    uint32_t width;              //  8  producer: width of frame in data area
    uint32_t height;             // 12  producer: height of frame in data area
    uint32_t format;             // 16  producer: 1 = NV12
    uint64_t frame_counter;      // 20  producer: incremented AFTER a frame is fully written
    uint64_t timestamp_100ns;    // 28  producer: capture time (FILETIME UTC, 100ns units)
    uint32_t consumer_active;    // 36  DLL: 1 while stream running, 0 otherwise
    uint64_t consumer_heartbeat; // 40  DLL: FILETIME UTC (100ns) updated ~every 100ms..1s while active
    uint32_t req_width;          // 48  DLL: width of current media type (what producer should send)
    uint32_t req_height;         // 52  DLL: height of current media type
    uint32_t req_fps;            // 56  DLL: frame rate numerator (30)
    uint32_t capacity;           // 60  DLL: bytes available for frame data (12,441,600)
};
#pragma pack(pop)
static_assert(sizeof(PsvcHeader) == PSVC_HEADER_SIZE, "header must be 64 bytes");

void VLog(const wchar_t* fmt, ...);
