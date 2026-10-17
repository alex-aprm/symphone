#include "shm.h"
#include <sddl.h>

static SharedFrame g_shared;
static SRWLOCK g_sharedLock = SRWLOCK_INIT;
static ULONGLONG g_lastAttempt = 0;

SharedFrame& GetSharedFrame() { return g_shared; }

uint64_t FileTimeNow()
{
    FILETIME ft;
    GetSystemTimePreciseAsFileTime(&ft);
    return ((uint64_t)ft.dwHighDateTime << 32) | ft.dwLowDateTime;
}

bool SharedFrame::Ensure()
{
    if (hdr) return true;
    AcquireSRWLockExclusive(&g_sharedLock);
    if (hdr) { ReleaseSRWLockExclusive(&g_sharedLock); return true; }

    // don't hammer CreateFileMapping (e.g. in processes without SeCreateGlobalPrivilege)
    ULONGLONG now = GetTickCount64();
    if (g_lastAttempt && now - g_lastAttempt < 2000) { ReleaseSRWLockExclusive(&g_sharedLock); return false; }
    g_lastAttempt = now;

    bool created = false;
    HANDLE m = OpenFileMappingW(FILE_MAP_READ | FILE_MAP_WRITE, FALSE, PSVC_MAPPING_NAME);
    if (!m)
    {
        DWORD openErr = GetLastError();
        SECURITY_ATTRIBUTES sa{ sizeof(sa), nullptr, FALSE };
        PSECURITY_DESCRIPTOR sd = nullptr;
        if (ConvertStringSecurityDescriptorToSecurityDescriptorW(PSVC_SDDL, SDDL_REVISION_1, &sd, nullptr))
            sa.lpSecurityDescriptor = sd;
        m = CreateFileMappingW(INVALID_HANDLE_VALUE, &sa, PAGE_READWRITE, 0, PSVC_MAPPING_SIZE, PSVC_MAPPING_NAME);
        DWORD err = GetLastError();
        if (m) created = (err != ERROR_ALREADY_EXISTS);
        else VLog(L"shm: OpenFileMapping err=%lu, CreateFileMapping failed err=%lu", openErr, err);
        if (!frameEvent)
        {
            frameEvent = CreateEventW(&sa, FALSE, FALSE, PSVC_EVENT_NAME);
            if (!frameEvent) frameEvent = OpenEventW(SYNCHRONIZE | EVENT_MODIFY_STATE, FALSE, PSVC_EVENT_NAME);
        }
        if (sd) LocalFree(sd);
    }
    if (!frameEvent)
        frameEvent = OpenEventW(SYNCHRONIZE | EVENT_MODIFY_STATE, FALSE, PSVC_EVENT_NAME);

    if (m)
    {
        void* view = MapViewOfFile(m, FILE_MAP_READ | FILE_MAP_WRITE, 0, 0, PSVC_MAPPING_SIZE);
        if (!view)
        {
            VLog(L"shm: MapViewOfFile failed err=%lu", GetLastError());
            CloseHandle(m);
        }
        else
        {
            mapping = m;
            hdr = (PsvcHeader*)view;
            data = (BYTE*)view + PSVC_HEADER_SIZE;
            if (created || hdr->magic != PSVC_MAGIC)
            {
                ZeroMemory(hdr, PSVC_HEADER_SIZE);
                hdr->version = PSVC_VERSION;
                hdr->req_width = 1920;
                hdr->req_height = 1080;
                hdr->req_fps = 30;
                hdr->capacity = PSVC_FRAME_CAPACITY;
                MemoryBarrier();
                hdr->magic = PSVC_MAGIC;
            }
            VLog(L"shm: %s %s (event=%p)", created ? L"created" : L"opened", PSVC_MAPPING_NAME, frameEvent);
        }
    }
    ReleaseSRWLockExclusive(&g_sharedLock);
    return hdr != nullptr;
}

void SharedFrame::Close()
{
    if (hdr) UnmapViewOfFile(hdr);
    hdr = nullptr; data = nullptr;
    if (mapping) CloseHandle(mapping);
    mapping = nullptr;
    if (frameEvent) CloseHandle(frameEvent);
    frameEvent = nullptr;
}
