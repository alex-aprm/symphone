// vddctl: install the Virtual Display Driver device node and attach/detach/configure its monitor.
//
//   vddctl install <path\MttVDD.inf>   (admin) create Root\MttVDD device node + install driver (like devcon install)
//   vddctl uninstall                    (admin) remove Root\MttVDD device nodes
//   vddctl list                         JSON: virtual monitor state / desktop rect / GDI name
//   vddctl attach [W H [HZ]]            add the virtual monitor to the desktop (no admin); optional mode
//   vddctl mode W H [HZ]                change its mode
//   vddctl detach                       remove it from the desktop (it stops receiving windows)
//
// The virtual monitor is identified by the EDID manufacturer "MTT" in its monitor device path.
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <setupapi.h>
#include <newdev.h>
#include <devguid.h>
#include <cfgmgr32.h>
#include <shellscalingapi.h>
#include <cstdio>
#include <string>
#include <vector>
#pragma comment(lib, "setupapi.lib")
#pragma comment(lib, "newdev.lib")
#pragma comment(lib, "user32.lib")
#pragma comment(lib, "shcore.lib")

static const wchar_t* HWID = L"Root\\MttVDD";

static std::wstring jsonEsc(const std::wstring& s) {
    std::wstring o;
    for (wchar_t c : s) { if (c == L'\\' || c == L'"') o += L'\\'; o += c; }
    return o;
}

// ------------------------------------------------------------------ device node
static int findDevices(std::vector<SP_DEVINFO_DATA>& out, HDEVINFO& set) {
    set = SetupDiGetClassDevsW(&GUID_DEVCLASS_DISPLAY, nullptr, nullptr, 0);
    if (set == INVALID_HANDLE_VALUE) return 0;
    SP_DEVINFO_DATA d{ sizeof(d) };
    for (DWORD i = 0; SetupDiEnumDeviceInfo(set, i, &d); i++) {
        wchar_t buf[1024]{}; DWORD type = 0;
        if (!SetupDiGetDeviceRegistryPropertyW(set, &d, SPDRP_HARDWAREID, &type, (BYTE*)buf, sizeof(buf) - 4, nullptr)) continue;
        for (wchar_t* p = buf; *p; p += wcslen(p) + 1)
            if (_wcsicmp(p, HWID) == 0) { out.push_back(d); break; }
    }
    return (int)out.size();
}

static int install(const wchar_t* inf) {
    wchar_t full[MAX_PATH];
    GetFullPathNameW(inf, MAX_PATH, full, nullptr);
    std::vector<SP_DEVINFO_DATA> devs; HDEVINFO set;
    int n = findDevices(devs, set);
    if (set != INVALID_HANDLE_VALUE) SetupDiDestroyDeviceInfoList(set);
    if (n == 0) {
        HDEVINFO s = SetupDiCreateDeviceInfoList(&GUID_DEVCLASS_DISPLAY, nullptr);
        SP_DEVINFO_DATA d{ sizeof(d) };
        if (!SetupDiCreateDeviceInfoW(s, L"Display", &GUID_DEVCLASS_DISPLAY, nullptr, nullptr, DICD_GENERATE_ID, &d)) {
            wprintf(L"create devinfo failed %lu\n", GetLastError()); return 2;
        }
        wchar_t ids[64]{}; wcscpy_s(ids, HWID);  // REG_MULTI_SZ, double-NUL terminated by zero init
        if (!SetupDiSetDeviceRegistryPropertyW(s, &d, SPDRP_HARDWAREID, (BYTE*)ids, (DWORD)((wcslen(ids) + 2) * sizeof(wchar_t)))) {
            wprintf(L"set hwid failed %lu\n", GetLastError()); return 2;
        }
        if (!SetupDiCallClassInstaller(DIF_REGISTERDEVICE, s, &d)) {
            wprintf(L"register device failed %lu\n", GetLastError()); return 2;
        }
        SetupDiDestroyDeviceInfoList(s);
        wprintf(L"device node created\n");
    } else {
        wprintf(L"device node exists (%d)\n", n);
    }
    BOOL reboot = FALSE;
    if (!UpdateDriverForPlugAndPlayDevicesW(nullptr, HWID, full, INSTALLFLAG_FORCE, &reboot)) {
        wprintf(L"UpdateDriverForPlugAndPlayDevices failed %lu\n", GetLastError()); return 3;
    }
    wprintf(L"driver installed%s\n", reboot ? L" (reboot required)" : L"");
    return 0;
}

static int uninstall() {
    std::vector<SP_DEVINFO_DATA> devs; HDEVINFO set;
    findDevices(devs, set);
    for (auto& d : devs) {
        if (SetupDiCallClassInstaller(DIF_REMOVE, set, &d)) wprintf(L"removed\n");
        else wprintf(L"remove failed %lu\n", GetLastError());
    }
    if (set != INVALID_HANDLE_VALUE) SetupDiDestroyDeviceInfoList(set);
    return 0;
}

// ------------------------------------------------------------------ display topology
struct Target {
    bool found = false;
    LUID adapter{}; UINT32 id = 0;
    std::wstring friendly, path;
};

static bool isVdd(const LUID& a, UINT32 id, Target& t) {
    DISPLAYCONFIG_TARGET_DEVICE_NAME tn{};
    tn.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_NAME;
    tn.header.size = sizeof(tn);
    tn.header.adapterId = a; tn.header.id = id;
    if (DisplayConfigGetDeviceInfo(&tn.header) != ERROR_SUCCESS) return false;
    std::wstring p = tn.monitorDevicePath;
    for (auto& c : p) c = towupper(c);
    if (p.find(L"#MTT") == std::wstring::npos) return false;
    t.found = true; t.adapter = a; t.id = id;
    t.friendly = tn.monitorFriendlyDeviceName; t.path = tn.monitorDevicePath;
    return true;
}

static bool query(UINT32 flags, std::vector<DISPLAYCONFIG_PATH_INFO>& paths, std::vector<DISPLAYCONFIG_MODE_INFO>& modes) {
    for (int tries = 0; tries < 5; tries++) {
        UINT32 np = 0, nm = 0;
        if (GetDisplayConfigBufferSizes(flags, &np, &nm) != ERROR_SUCCESS) return false;
        paths.resize(np); modes.resize(nm);
        LONG r = QueryDisplayConfig(flags, &np, paths.data(), &nm, modes.data(), nullptr);
        if (r == ERROR_INSUFFICIENT_BUFFER) continue;
        if (r != ERROR_SUCCESS) return false;
        paths.resize(np); modes.resize(nm);
        return true;
    }
    return false;
}

static std::wstring gdiName(const DISPLAYCONFIG_PATH_INFO& p) {
    DISPLAYCONFIG_SOURCE_DEVICE_NAME sn{};
    sn.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME;
    sn.header.size = sizeof(sn);
    sn.header.adapterId = p.sourceInfo.adapterId; sn.header.id = p.sourceInfo.id;
    if (DisplayConfigGetDeviceInfo(&sn.header) != ERROR_SUCCESS) return L"";
    return sn.viewGdiDeviceName;
}

// ---- per-monitor scale (the DisplayConfig DPI-scale info types used by Settings > Display; undocumented)
struct DpiGet { DISPLAYCONFIG_DEVICE_INFO_HEADER header; INT32 minRel, curRel, maxRel; };
struct DpiSet { DISPLAYCONFIG_DEVICE_INFO_HEADER header; INT32 rel; };
static const int DPI_VALS[] = { 100, 125, 150, 175, 200, 225, 250, 300, 350, 400, 450, 500 };
static const int NDPI = sizeof(DPI_VALS) / sizeof(DPI_VALS[0]);

static bool readDpi(const DISPLAYCONFIG_PATH_INFO& p, DpiGet& g) {
    g = {};
    g.header.type = (DISPLAYCONFIG_DEVICE_INFO_TYPE)-3;
    g.header.size = sizeof(g);
    g.header.adapterId = p.sourceInfo.adapterId; g.header.id = p.sourceInfo.id;
    return DisplayConfigGetDeviceInfo(&g.header) == ERROR_SUCCESS && abs(g.minRel) < NDPI;
}

static bool getScale(const DISPLAYCONFIG_PATH_INFO& p, int& cur, int& rec, int& mn, int& mx) {
    DpiGet g;
    if (!readDpi(p, g)) return false;
    int ri = abs(g.minRel);
    rec = DPI_VALS[ri];
    int ci = ri + g.curRel; if (ci < 0 || ci >= NDPI) return false;
    cur = DPI_VALS[ci];
    mn = DPI_VALS[0];
    int mi = ri + g.maxRel; mx = DPI_VALS[mi < NDPI ? mi : NDPI - 1];
    return true;
}

static bool setScale(const DISPLAYCONFIG_PATH_INFO& p, int pct) {
    DpiGet g;
    if (!readDpi(p, g)) return false;
    int ri = abs(g.minRel), want = -1;
    for (int i = 0; i < NDPI; i++) if (DPI_VALS[i] == pct) want = i;
    if (want < 0) return false;
    int rel = want - ri;
    if (rel < g.minRel) rel = g.minRel;
    if (rel > g.maxRel) rel = g.maxRel;
    DpiSet st{};
    st.header.type = (DISPLAYCONFIG_DEVICE_INFO_TYPE)-4;
    st.header.size = sizeof(st);
    st.header.adapterId = p.sourceInfo.adapterId; st.header.id = p.sourceInfo.id;
    st.rel = rel;
    return DisplayConfigSetDeviceInfo(&st.header) == ERROR_SUCCESS;
}

static int scaleCmd(int pct) {
    std::vector<DISPLAYCONFIG_PATH_INFO> paths; std::vector<DISPLAYCONFIG_MODE_INFO> modes;
    Target t;
    if (!query(QDC_ONLY_ACTIVE_PATHS, paths, modes)) return 2;
    for (auto& p : paths) {
        if (!isVdd(p.targetInfo.adapterId, p.targetInfo.id, t)) continue;
        if (pct > 0 && !setScale(p, pct)) { fwprintf(stderr, L"set scale failed\n"); return 3; }
        return 0;
    }
    fwprintf(stderr, L"virtual monitor not active\n");
    return 4;
}

static int list() {
    std::vector<DISPLAYCONFIG_PATH_INFO> paths; std::vector<DISPLAYCONFIG_MODE_INFO> modes;
    Target t;
    if (query(QDC_ONLY_ACTIVE_PATHS, paths, modes)) {
        for (auto& p : paths) {
            if (!isVdd(p.targetInfo.adapterId, p.targetInfo.id, t)) continue;
            std::wstring gdi = gdiName(p);
            DEVMODEW dm{}; dm.dmSize = sizeof(dm);
            EnumDisplaySettingsExW(gdi.c_str(), ENUM_CURRENT_SETTINGS, &dm, 0);
            HMONITOR hm = nullptr;
            POINT c{ dm.dmPosition.x + (LONG)dm.dmPelsWidth / 2, dm.dmPosition.y + (LONG)dm.dmPelsHeight / 2 };
            hm = MonitorFromPoint(c, MONITOR_DEFAULTTONULL);
            UINT dx = 96, dy = 96;
            if (hm) GetDpiForMonitor(hm, MDT_EFFECTIVE_DPI, &dx, &dy);
            int cur = 0, rec = 0, mn = 0, mx = 0;
            getScale(p, cur, rec, mn, mx);
            wprintf(L"{\"found\":true,\"active\":true,\"gdi\":\"%s\",\"name\":\"%s\",\"x\":%ld,\"y\":%ld,\"w\":%lu,\"h\":%lu,\"hz\":%lu,\"dpi\":%u,\"orientation\":%lu,\"scale\":%d,\"scale_recommended\":%d,\"scale_max\":%d}\n",
                jsonEsc(gdi).c_str(), jsonEsc(t.friendly).c_str(), dm.dmPosition.x, dm.dmPosition.y, dm.dmPelsWidth, dm.dmPelsHeight,
                dm.dmDisplayFrequency, dx, dm.dmDisplayOrientation, cur, rec, mx);
            return 0;
        }
    }
    if (query(QDC_ALL_PATHS, paths, modes)) {
        for (auto& p : paths)
            if (isVdd(p.targetInfo.adapterId, p.targetInfo.id, t)) {
                wprintf(L"{\"found\":true,\"active\":false,\"name\":\"%s\"}\n", jsonEsc(t.friendly).c_str());
                return 0;
            }
    }
    wprintf(L"{\"found\":false,\"active\":false}\n");
    return 0;
}

static int setMode(int w, int h, int hz) {
    std::vector<DISPLAYCONFIG_PATH_INFO> paths; std::vector<DISPLAYCONFIG_MODE_INFO> modes;
    Target t;
    if (!query(QDC_ONLY_ACTIVE_PATHS, paths, modes)) return 2;
    for (auto& p : paths) {
        if (!isVdd(p.targetInfo.adapterId, p.targetInfo.id, t)) continue;
        std::wstring gdi = gdiName(p);
        DEVMODEW dm{}; dm.dmSize = sizeof(dm);
        dm.dmPelsWidth = w; dm.dmPelsHeight = h;
        dm.dmFields = DM_PELSWIDTH | DM_PELSHEIGHT;
        if (hz > 0) { dm.dmDisplayFrequency = hz; dm.dmFields |= DM_DISPLAYFREQUENCY; }
        LONG r = ChangeDisplaySettingsExW(gdi.c_str(), &dm, nullptr, CDS_UPDATEREGISTRY | CDS_GLOBAL, nullptr);
        if (r != DISP_CHANGE_SUCCESSFUL && hz > 0) {  // retry without the refresh rate
            dm.dmFields &= ~DM_DISPLAYFREQUENCY;
            r = ChangeDisplaySettingsExW(gdi.c_str(), &dm, nullptr, CDS_UPDATEREGISTRY | CDS_GLOBAL, nullptr);
        }
        if (r != DISP_CHANGE_SUCCESSFUL) { fwprintf(stderr, L"ChangeDisplaySettingsEx %ld\n", r); return 3; }
        return 0;
    }
    fwprintf(stderr, L"virtual monitor not active\n");
    return 4;
}

static int attach() {
    std::vector<DISPLAYCONFIG_PATH_INFO> act, all; std::vector<DISPLAYCONFIG_MODE_INFO> am, alm;
    Target t;
    if (!query(QDC_ONLY_ACTIVE_PATHS, act, am)) return 2;
    for (auto& p : act) if (isVdd(p.targetInfo.adapterId, p.targetInfo.id, t)) return 0;  // already attached
    if (!query(QDC_ALL_PATHS, all, alm)) return 2;
    // Paths for the same target are listed per possible source; pick one whose source is not in use.
    for (auto& p : all) {
        if (!isVdd(p.targetInfo.adapterId, p.targetInfo.id, t)) continue;
        bool srcUsed = false;
        for (auto& a : act)
            if (a.sourceInfo.id == p.sourceInfo.id && a.sourceInfo.adapterId.LowPart == p.sourceInfo.adapterId.LowPart &&
                a.sourceInfo.adapterId.HighPart == p.sourceInfo.adapterId.HighPart) srcUsed = true;
        if (srcUsed) continue;
        std::vector<DISPLAYCONFIG_PATH_INFO> np = act;
        DISPLAYCONFIG_PATH_INFO q = p;
        q.flags |= DISPLAYCONFIG_PATH_ACTIVE;
        q.sourceInfo.modeInfoIdx = DISPLAYCONFIG_PATH_MODE_IDX_INVALID;
        q.targetInfo.modeInfoIdx = DISPLAYCONFIG_PATH_MODE_IDX_INVALID;
        np.push_back(q);
        // Let Windows restore position/scale for this monitor from its persistence database.
        LONG r = SetDisplayConfig((UINT32)np.size(), np.data(), 0, nullptr,
            SDC_APPLY | SDC_TOPOLOGY_SUPPLIED | SDC_ALLOW_PATH_ORDER_CHANGES);
        if (r != ERROR_SUCCESS) {
            r = SetDisplayConfig((UINT32)np.size(), np.data(), (UINT32)am.size(), am.data(),
                SDC_APPLY | SDC_USE_SUPPLIED_DISPLAY_CONFIG | SDC_ALLOW_CHANGES | SDC_SAVE_TO_DATABASE);
        }
        if (r == ERROR_SUCCESS) return 0;
        fwprintf(stderr, L"SetDisplayConfig %ld\n", r);
    }
    // Fallback: extend topology (attaches every connected display).
    if (!t.found) { fwprintf(stderr, L"virtual monitor not present (driver not installed?)\n"); return 4; }
    LONG r = SetDisplayConfig(0, nullptr, 0, nullptr, SDC_APPLY | SDC_TOPOLOGY_EXTEND);
    return r == ERROR_SUCCESS ? 0 : 5;
}

static int detach() {
    std::vector<DISPLAYCONFIG_PATH_INFO> act; std::vector<DISPLAYCONFIG_MODE_INFO> am;
    Target t;
    if (!query(QDC_ONLY_ACTIVE_PATHS, act, am)) return 2;
    bool any = false;
    for (auto& p : act) if (isVdd(p.targetInfo.adapterId, p.targetInfo.id, t)) { p.flags &= ~DISPLAYCONFIG_PATH_ACTIVE; any = true; }
    if (!any) return 0;
    LONG r = SetDisplayConfig((UINT32)act.size(), act.data(), (UINT32)am.size(), am.data(),
        SDC_APPLY | SDC_USE_SUPPLIED_DISPLAY_CONFIG | SDC_ALLOW_CHANGES | SDC_SAVE_TO_DATABASE);
    if (r != ERROR_SUCCESS) { fwprintf(stderr, L"SetDisplayConfig %ld\n", r); return 3; }
    return 0;
}

int wmain(int argc, wchar_t** argv) {
    SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
    if (argc < 2) { fwprintf(stderr, L"usage: vddctl install <inf>|uninstall|list|attach [w h [hz]]|mode w h [hz]|detach\n"); return 1; }
    std::wstring c = argv[1];
    if (c == L"install" && argc > 2) return install(argv[2]);
    if (c == L"uninstall") return uninstall();
    if (c == L"list") return list();
    if (c == L"detach") return detach();
    if (c == L"scale") {
        int r = scaleCmd(argc > 2 ? _wtoi(argv[2]) : 0);
        if (r == 0) { Sleep(200); list(); }
        return r;
    }
    if (c == L"attach") {
        int r = attach();
        if (r == 0 && argc > 3) { Sleep(300); r = setMode(_wtoi(argv[2]), _wtoi(argv[3]), argc > 4 ? _wtoi(argv[4]) : 0); }
        if (r == 0) list();
        return r;
    }
    if (c == L"mode" && argc > 3) {
        int r = setMode(_wtoi(argv[2]), _wtoi(argv[3]), argc > 4 ? _wtoi(argv[4]) : 0);
        if (r == 0) list();
        return r;
    }
    fwprintf(stderr, L"bad args\n");
    return 1;
}
