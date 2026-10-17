// Process-wide shared memory (frame transport) used by the media source.
#pragma once
#include "common.h"

struct SharedFrame
{
    HANDLE mapping = nullptr;
    HANDLE frameEvent = nullptr;
    PsvcHeader* hdr = nullptr;
    BYTE* data = nullptr;

    // Opens (or creates, with a permissive DACL) Global\PhoneSyncVCam + Global\PhoneSyncVCamFrame.
    // Safe to call repeatedly; returns true when mapped.
    bool Ensure();
    void Close();
};

SharedFrame& GetSharedFrame();
uint64_t FileTimeNow();
