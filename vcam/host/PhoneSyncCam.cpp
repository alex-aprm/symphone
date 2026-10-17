// PhoneSyncCam.exe - installs / runs / inspects the PhoneSync Media Foundation virtual camera.
// Virtual camera creation follows smourier/VCamSample (MIT).
//
//   PhoneSyncCam install   [--name "<name>"]   (admin) register COM DLL in HKLM + System-lifetime camera
//   PhoneSyncCam uninstall [--name "<name>"]   (admin) remove camera + unregister COM DLL
//   PhoneSyncCam run       [--name "<name>"] [--seconds N]  session camera while the process runs
//   PhoneSyncCam status                         list MF video capture devices
//   PhoneSyncCam grab      [--name "<substr>"] [--out file.bmp] [--frames N]  capture one frame via MF
#include <initguid.h>
#include "common.h"
#include <mfapi.h>
#include <mfidl.h>
#include <mferror.h>
#include <mfreadwrite.h>
#include <mfvirtualcamera.h>
#include <shellapi.h>
#include <stdio.h>
#include <string>
#include <vector>

static const wchar_t* kDefaultName = L"PhoneSync Camera";
static HANDLE g_quit = nullptr;

void VLog(const wchar_t*, ...) {} // the exe logs to the console only

static std::wstring ExeDir()
{
    wchar_t p[MAX_PATH];
    GetModuleFileNameW(nullptr, p, MAX_PATH);
    std::wstring s(p);
    return s.substr(0, s.find_last_of(L'\\'));
}

static bool IsElevated()
{
    HANDLE tok;
    TOKEN_ELEVATION e{};
    DWORD sz;
    if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &tok)) return false;
    BOOL ok = GetTokenInformation(tok, TokenElevation, &e, sizeof(e), &sz);
    CloseHandle(tok);
    return ok && e.TokenIsElevated;
}

static HRESULT CallDllReg(bool reg)
{
    std::wstring dll = ExeDir() + L"\\PhoneSyncVCam.dll";
    HMODULE m = LoadLibraryW(dll.c_str());
    if (!m) { wprintf(L"cannot load %s (err %lu)\n", dll.c_str(), GetLastError()); return HRESULT_FROM_WIN32(GetLastError()); }
    typedef HRESULT(STDAPICALLTYPE* Fn)();
    Fn fn = (Fn)GetProcAddress(m, reg ? "DllRegisterServer" : "DllUnregisterServer");
    HRESULT hr = fn ? fn() : E_NOTIMPL;
    FreeLibrary(m);
    wprintf(L"%s %s: 0x%08X\n", reg ? L"DllRegisterServer" : L"DllUnregisterServer", dll.c_str(), hr);
    return hr;
}

static std::wstring RegisteredDllPath()
{
    wchar_t buf[MAX_PATH] = L"";
    DWORD sz = sizeof(buf);
    if (RegGetValueW(HKEY_LOCAL_MACHINE, L"Software\\Classes\\CLSID\\" PSVC_CLSID_STRING L"\\InprocServer32", nullptr,
        RRF_RT_REG_SZ, nullptr, buf, &sz) == ERROR_SUCCESS)
        return buf;
    return L"";
}

static HRESULT CreateVCam(const std::wstring& name, MFVirtualCameraLifetime life, MFVirtualCameraAccess access, IMFVirtualCamera** out)
{
    HRESULT hr = MFCreateVirtualCamera(MFVirtualCameraType_SoftwareCameraSource, life, access,
        name.c_str(), PSVC_CLSID_STRING, nullptr, 0, out);
    wprintf(L"MFCreateVirtualCamera('%s', %s, %s): 0x%08X\n", name.c_str(),
        life == MFVirtualCameraLifetime_System ? L"System" : L"Session",
        access == MFVirtualCameraAccess_AllUsers ? L"AllUsers" : L"CurrentUser", hr);
    return hr;
}

struct DeviceInfo { std::wstring name, link; };

static std::vector<DeviceInfo> EnumCams()
{
    std::vector<DeviceInfo> res;
    IMFAttributes* a = nullptr;
    if (FAILED(MFCreateAttributes(&a, 1))) return res;
    a->SetGUID(MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE, MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE_VIDCAP_GUID);
    IMFActivate** devs = nullptr;
    UINT32 n = 0;
    if (SUCCEEDED(MFEnumDeviceSources(a, &devs, &n)))
    {
        for (UINT32 i = 0; i < n; i++)
        {
            wchar_t* s = nullptr; UINT32 len;
            DeviceInfo d;
            if (SUCCEEDED(devs[i]->GetAllocatedString(MF_DEVSOURCE_ATTRIBUTE_FRIENDLY_NAME, &s, &len))) { d.name = s; CoTaskMemFree(s); }
            if (SUCCEEDED(devs[i]->GetAllocatedString(MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE_VIDCAP_SYMBOLIC_LINK, &s, &len))) { d.link = s; CoTaskMemFree(s); }
            res.push_back(d);
            devs[i]->Release();
        }
        CoTaskMemFree(devs);
    }
    a->Release();
    return res;
}

static int CmdStatus(const std::wstring& name)
{
    std::wstring dll = RegisteredDllPath();
    wprintf(L"COM source %s: %s\n", PSVC_CLSID_STRING, dll.empty() ? L"NOT registered (HKLM)" : dll.c_str());
    auto cams = EnumCams();
    wprintf(L"Media Foundation video capture devices (%zu):\n", cams.size());
    bool found = false;
    for (size_t i = 0; i < cams.size(); i++)
    {
        bool ours = cams[i].name == name || cams[i].name.find(L"PhoneSync") != std::wstring::npos;
        found |= ours;
        wprintf(L"  [%zu] %s%s\n       %s\n", i, cams[i].name.c_str(), ours ? L"   <== PhoneSync virtual camera" : L"", cams[i].link.c_str());
    }
    HANDLE m = OpenFileMappingW(FILE_MAP_READ, FALSE, PSVC_MAPPING_NAME);
    if (m)
    {
        auto* h = (PsvcHeader*)MapViewOfFile(m, FILE_MAP_READ, 0, 0, PSVC_HEADER_SIZE);
        if (h)
        {
            wprintf(L"Shared memory %s: magic=0x%08X active=%u req=%ux%u frame=%ux%u counter=%llu\n", PSVC_MAPPING_NAME,
                h->magic, h->consumer_active, h->req_width, h->req_height, h->width, h->height, h->frame_counter);
            UnmapViewOfFile(h);
        }
        CloseHandle(m);
    }
    else wprintf(L"Shared memory %s: not present (err %lu)\n", PSVC_MAPPING_NAME, GetLastError());
    wprintf(L"Virtual camera enumerable: %s\n", found ? L"YES" : L"NO");
    return found ? 0 : 1;
}

static int CmdInstall(const std::wstring& name)
{
    if (!IsElevated()) { wprintf(L"install requires administrator rights\n"); return 5; }
    HRESULT hr = CallDllReg(true);
    if (FAILED(hr)) return 2;
    IMFVirtualCamera* cam = nullptr;
    hr = CreateVCam(name, MFVirtualCameraLifetime_System, MFVirtualCameraAccess_AllUsers, &cam);
    if (FAILED(hr)) return 3;
    hr = cam->Start(nullptr);
    wprintf(L"IMFVirtualCamera::Start: 0x%08X\n", hr);
    // System lifetime: the camera persists after we release it (no Shutdown/Remove here).
    cam->Release();
    return SUCCEEDED(hr) ? 0 : 4;
}

static int CmdUninstall(const std::wstring& name, bool keepCom)
{
    if (!IsElevated()) { wprintf(L"uninstall requires administrator rights\n"); return 5; }
    int rc = 0;
    // re-creating with identical parameters yields the same device, which can then be removed
    IMFVirtualCamera* cam = nullptr;
    HRESULT hr = CreateVCam(name, MFVirtualCameraLifetime_System, MFVirtualCameraAccess_AllUsers, &cam);
    if (SUCCEEDED(hr))
    {
        hr = cam->Remove();
        wprintf(L"IMFVirtualCamera::Remove: 0x%08X\n", hr);
        if (FAILED(hr)) rc = 3;
        cam->Release();
    }
    else rc = 3;
    if (!keepCom) CallDllReg(false);
    return rc;
}

static BOOL WINAPI CtrlHandler(DWORD) { SetEvent(g_quit); return TRUE; }

static int CmdRun(const std::wstring& name, int seconds)
{
    if (RegisteredDllPath().empty()) wprintf(L"warning: COM source not registered in HKLM; run 'install' (admin) first\n");
    IMFVirtualCamera* cam = nullptr;
    HRESULT hr = CreateVCam(name, MFVirtualCameraLifetime_Session, MFVirtualCameraAccess_CurrentUser, &cam);
    if (FAILED(hr)) return 3;
    hr = cam->Start(nullptr);
    wprintf(L"IMFVirtualCamera::Start: 0x%08X\n", hr);
    if (FAILED(hr)) { cam->Remove(); cam->Release(); return 4; }
    g_quit = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    SetConsoleCtrlHandler(CtrlHandler, TRUE);
    wprintf(L"Camera '%s' running. Ctrl+C to stop.\n", name.c_str());
    fflush(stdout);
    WaitForSingleObject(g_quit, seconds > 0 ? (DWORD)seconds * 1000 : INFINITE);
    hr = cam->Remove();
    wprintf(L"IMFVirtualCamera::Remove: 0x%08X\n", hr);
    cam->Release();
    return 0;
}

static bool SaveBmp(const std::wstring& path, const BYTE* rgb32, LONG stride, UINT32 w, UINT32 h)
{
    BITMAPFILEHEADER bf{};
    BITMAPINFOHEADER bi{};
    bi.biSize = sizeof(bi); bi.biWidth = w; bi.biHeight = -(LONG)h; bi.biPlanes = 1; bi.biBitCount = 32; bi.biCompression = BI_RGB;
    bi.biSizeImage = w * h * 4;
    bf.bfType = 0x4D42; bf.bfOffBits = sizeof(bf) + sizeof(bi); bf.bfSize = bf.bfOffBits + bi.biSizeImage;
    FILE* f = nullptr;
    if (_wfopen_s(&f, path.c_str(), L"wb") || !f) return false;
    fwrite(&bf, sizeof(bf), 1, f);
    fwrite(&bi, sizeof(bi), 1, f);
    for (UINT32 y = 0; y < h; y++) fwrite(rgb32 + (LONGLONG)y * stride, 1, w * 4, f);
    fclose(f);
    return true;
}

static int GrabFromSource(IMFMediaSource* src, const std::wstring& out, int frames, UINT32 reqW, UINT32 reqH);

// loads PhoneSyncVCam.dll next to the exe directly (no registration / frame server) and reads frames
static int CmdSelfTest(const std::wstring& out, int frames, UINT32 reqW, UINT32 reqH)
{
    std::wstring dll = ExeDir() + L"\\PhoneSyncVCam.dll";
    HMODULE m = LoadLibraryW(dll.c_str());
    if (!m) { wprintf(L"cannot load %s\n", dll.c_str()); return 2; }
    typedef HRESULT(STDAPICALLTYPE* GetCO)(REFCLSID, REFIID, LPVOID*);
    auto gco = (GetCO)GetProcAddress(m, "DllGetClassObject");
    IClassFactory* cf = nullptr;
    HRESULT hr = gco(CLSID_PhoneSyncVCam, IID_PPV_ARGS(&cf));
    if (FAILED(hr)) { wprintf(L"DllGetClassObject 0x%08X\n", hr); return 2; }
    IMFActivate* act = nullptr;
    hr = cf->CreateInstance(nullptr, IID_PPV_ARGS(&act));
    cf->Release();
    if (FAILED(hr)) { wprintf(L"CreateInstance 0x%08X\n", hr); return 2; }
    IMFMediaSource* src = nullptr;
    hr = act->ActivateObject(IID_PPV_ARGS(&src));
    if (FAILED(hr)) { wprintf(L"ActivateObject 0x%08X\n", hr); return 2; }
    int rc = GrabFromSource(src, out, frames, reqW, reqH);
    act->DetachObject();
    act->Release();
    return rc;
}

static int CmdGrab(const std::wstring& nameSub, const std::wstring& out, int frames, UINT32 reqW, UINT32 reqH)
{
    IMFAttributes* a = nullptr;
    MFCreateAttributes(&a, 1);
    a->SetGUID(MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE, MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE_VIDCAP_GUID);
    IMFActivate** devs = nullptr; UINT32 n = 0;
    HRESULT hr = MFEnumDeviceSources(a, &devs, &n);
    a->Release();
    if (FAILED(hr)) { wprintf(L"MFEnumDeviceSources 0x%08X\n", hr); return 2; }
    IMFActivate* act = nullptr;
    for (UINT32 i = 0; i < n; i++)
    {
        wchar_t* s = nullptr; UINT32 len;
        if (!act && SUCCEEDED(devs[i]->GetAllocatedString(MF_DEVSOURCE_ATTRIBUTE_FRIENDLY_NAME, &s, &len)))
        {
            if (wcsstr(s, nameSub.c_str())) { act = devs[i]; act->AddRef(); wprintf(L"Using device '%s'\n", s); }
            CoTaskMemFree(s);
        }
        devs[i]->Release();
    }
    CoTaskMemFree(devs);
    if (!act) { wprintf(L"no device matching '%s'\n", nameSub.c_str()); return 2; }

    IMFMediaSource* src = nullptr;
    hr = act->ActivateObject(IID_PPV_ARGS(&src));
    if (FAILED(hr)) { wprintf(L"ActivateObject 0x%08X\n", hr); return 3; }
    int rc = GrabFromSource(src, out, frames, reqW, reqH);
    act->Release();
    return rc;
}

static int GrabFromSource(IMFMediaSource* src, const std::wstring& out, int frames, UINT32 reqW, UINT32 reqH)
{
    HRESULT hr;
    IMFAttributes* ra = nullptr;
    MFCreateAttributes(&ra, 2);
    ra->SetUINT32(MF_SOURCE_READER_ENABLE_VIDEO_PROCESSING, TRUE);
    IMFSourceReader* rd = nullptr;
    hr = MFCreateSourceReaderFromMediaSource(src, ra, &rd);
    ra->Release();
    if (FAILED(hr)) { wprintf(L"MFCreateSourceReaderFromMediaSource 0x%08X\n", hr); return 3; }

    // list native types, pick requested size
    for (DWORD i = 0;; i++)
    {
        IMFMediaType* t = nullptr;
        if (FAILED(rd->GetNativeMediaType((DWORD)MF_SOURCE_READER_FIRST_VIDEO_STREAM, i, &t))) break;
        UINT32 w = 0, h = 0, fn = 0, fd = 1; GUID sub{};
        MFGetAttributeSize(t, MF_MT_FRAME_SIZE, &w, &h);
        MFGetAttributeRatio(t, MF_MT_FRAME_RATE, &fn, &fd);
        t->GetGUID(MF_MT_SUBTYPE, &sub);
        wprintf(L"  native type %lu: %ux%u @%u/%u fourcc=%.4hs\n", i, w, h, fn, fd, (const char*)&sub.Data1);
        if (reqW && w == reqW && h == reqH) rd->SetCurrentMediaType((DWORD)MF_SOURCE_READER_FIRST_VIDEO_STREAM, nullptr, t);
        t->Release();
    }
    IMFMediaType* outType = nullptr;
    MFCreateMediaType(&outType);
    outType->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Video);
    outType->SetGUID(MF_MT_SUBTYPE, MFVideoFormat_NV12);
    hr = rd->SetCurrentMediaType((DWORD)MF_SOURCE_READER_FIRST_VIDEO_STREAM, nullptr, outType);
    outType->Release();
    if (FAILED(hr)) { wprintf(L"SetCurrentMediaType NV12 0x%08X\n", hr); return 3; }
    IMFMediaType* cur = nullptr;
    rd->GetCurrentMediaType((DWORD)MF_SOURCE_READER_FIRST_VIDEO_STREAM, &cur);
    UINT32 w = 0, h = 0;
    MFGetAttributeSize(cur, MF_MT_FRAME_SIZE, &w, &h);
    cur->Release();
    wprintf(L"Reading %d frames at %ux%u...\n", frames, w, h);

    IMFSample* last = nullptr;
    ULONGLONG t0 = 0;
    int got = 0;
    for (int i = 0; i < frames; i++)
    {
        DWORD si, flags; LONGLONG ts; IMFSample* s = nullptr;
        hr = rd->ReadSample((DWORD)MF_SOURCE_READER_FIRST_VIDEO_STREAM, 0, &si, &flags, &ts, &s);
        if (FAILED(hr)) { wprintf(L"ReadSample 0x%08X\n", hr); break; }
        if (s) { if (last) last->Release(); last = s; if (!got++) t0 = GetTickCount64(); }
    }
    double secs = (GetTickCount64() - t0) / 1000.0;
    wprintf(L"Got %d samples; after the first: %.2fs (%.1f fps)\n", got, secs, secs > 0 ? (got - 1) / secs : 0.0);
    int rc = 4;
    if (last)
    {
        IMFMediaBuffer* b = nullptr;
        if (SUCCEEDED(last->ConvertToContiguousBuffer(&b)))
        {
            BYTE* p; DWORD max, len;
            if (SUCCEEDED(b->Lock(&p, &max, &len)))
            {
                // NV12 -> BGRA (BT.601 limited range)
                std::vector<BYTE> rgb((size_t)w * h * 4);
                const BYTE* Y = p; const BYTE* UV = p + (size_t)w * h;
                for (UINT32 y = 0; y < h; y++)
                    for (UINT32 x = 0; x < w; x++)
                    {
                        int c = Y[(size_t)y * w + x] - 16, d = UV[(size_t)(y / 2) * w + (x & ~1u)] - 128, e = UV[(size_t)(y / 2) * w + (x & ~1u) + 1] - 128;
                        auto clip = [](int v) { return (BYTE)(v < 0 ? 0 : v > 255 ? 255 : v); };
                        BYTE* o = &rgb[((size_t)y * w + x) * 4];
                        o[2] = clip((298 * c + 409 * e + 128) >> 8);
                        o[1] = clip((298 * c - 100 * d - 208 * e + 128) >> 8);
                        o[0] = clip((298 * c + 516 * d + 128) >> 8);
                        o[3] = 255;
                    }
                wprintf(L"center Y=%u U=%u V=%u\n", Y[(size_t)(h / 2) * w + w / 2], UV[(size_t)(h / 4) * w + (w / 2 & ~1u)], UV[(size_t)(h / 4) * w + (w / 2 & ~1u) + 1]);
                if (len >= (DWORD)(w * h * 3 / 2) && SaveBmp(out, rgb.data(), w * 4, w, h)) { wprintf(L"Saved %s\n", out.c_str()); rc = 0; }
                b->Unlock();
            }
            b->Release();
        }
        last->Release();
    }
    rd->Release();
    src->Shutdown();
    src->Release();
    return rc;
}

int wmain(int argc, wchar_t** argv)
{
    if (argc < 2)
    {
        wprintf(L"usage: PhoneSyncCam install|uninstall|run|status|grab [--name \"<name>\"] [--seconds N] [--out f.bmp] [--frames N] [--size WxH] [--keep-com]\n");
        return 1;
    }
    std::wstring cmd = argv[1];
    std::wstring name = kDefaultName, out = L"grab.bmp";
    int seconds = 0, frames = 30;
    bool keepCom = false, nameGiven = false;
    UINT32 reqW = 0, reqH = 0;
    for (int i = 2; i < argc; i++)
    {
        std::wstring a = argv[i];
        if (a == L"--name" && i + 1 < argc) { name = argv[++i]; nameGiven = true; }
        else if (a == L"--seconds" && i + 1 < argc) seconds = _wtoi(argv[++i]);
        else if (a == L"--out" && i + 1 < argc) out = argv[++i];
        else if (a == L"--frames" && i + 1 < argc) frames = _wtoi(argv[++i]);
        else if (a == L"--size" && i + 1 < argc) swscanf_s(argv[++i], L"%ux%u", &reqW, &reqH);
        else if (a == L"--keep-com") keepCom = true;
    }
    HRESULT hr = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    hr = MFStartup(MF_VERSION);
    if (FAILED(hr)) { wprintf(L"MFStartup 0x%08X\n", hr); return 1; }
    int rc = 1;
    if (cmd == L"install") rc = CmdInstall(name);
    else if (cmd == L"uninstall") rc = CmdUninstall(name, keepCom);
    else if (cmd == L"run") rc = CmdRun(name, seconds);
    else if (cmd == L"status") rc = CmdStatus(name);
    else if (cmd == L"selftest") rc = CmdSelfTest(out, frames, reqW, reqH);
    else if (cmd == L"grab") rc = CmdGrab(nameGiven ? name : L"PhoneSync", out, frames, reqW, reqH);
    else wprintf(L"unknown command %s\n", cmd.c_str());
    MFShutdown();
    CoUninitialize();
    return rc;
}




