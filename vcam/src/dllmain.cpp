// COM plumbing for PhoneSyncVCam.dll.
// Registration logic adapted from smourier/VCamSample dllmain.cpp (MIT).
#include <initguid.h>
#include "source.h"
#include "shm.h"
#include <new>

volatile LONG g_objectCount = 0;
static volatile LONG g_lockCount = 0;
static HMODULE g_module = nullptr;

BOOL APIENTRY DllMain(HMODULE hModule, DWORD reason, LPVOID)
{
    if (reason == DLL_PROCESS_ATTACH)
    {
        g_module = hModule;
        DisableThreadLibraryCalls(hModule);
        VLog(L"DLL_PROCESS_ATTACH cmdline='%s'", GetCommandLineW());
    }
    else if (reason == DLL_PROCESS_DETACH)
    {
        VLog(L"DLL_PROCESS_DETACH");
    }
    return TRUE;
}

class ClassFactory : public IClassFactory
{
    LONG m_ref = 1;
public:
    ClassFactory() { InterlockedIncrement(&g_objectCount); }
    ~ClassFactory() { InterlockedDecrement(&g_objectCount); }
    STDMETHODIMP QueryInterface(REFIID riid, void** ppv) override
    {
        if (!ppv) return E_POINTER;
        *ppv = nullptr;
        if (riid == IID_IUnknown || riid == IID_IClassFactory) { *ppv = this; AddRef(); return S_OK; }
        return E_NOINTERFACE;
    }
    STDMETHODIMP_(ULONG) AddRef() override { return InterlockedIncrement(&m_ref); }
    STDMETHODIMP_(ULONG) Release() override { LONG r = InterlockedDecrement(&m_ref); if (!r) delete this; return r; }
    STDMETHODIMP CreateInstance(IUnknown* outer, REFIID riid, void** ppv) override
    {
        if (!ppv) return E_POINTER;
        *ppv = nullptr;
        if (outer) return CLASS_E_NOAGGREGATION;
        Activator* a = new (std::nothrow) Activator();
        if (!a) return E_OUTOFMEMORY;
        HRESULT hr = a->Initialize();
        if (SUCCEEDED(hr)) hr = a->QueryInterface(riid, ppv);
        a->Release();
        wchar_t g[64]; GuidToString(riid, g, 64);
        VLog(L"ClassFactory::CreateInstance %s hr=0x%08X", g, hr);
        return hr;
    }
    STDMETHODIMP LockServer(BOOL lock) override
    {
        if (lock) InterlockedIncrement(&g_lockCount); else InterlockedDecrement(&g_lockCount);
        return S_OK;
    }
};

STDAPI DllCanUnloadNow()
{
    return (g_objectCount == 0 && g_lockCount == 0) ? S_OK : S_FALSE;
}

STDAPI DllGetClassObject(REFCLSID rclsid, REFIID riid, LPVOID* ppv)
{
    if (!ppv) return E_POINTER;
    *ppv = nullptr;
    if (rclsid != CLSID_PhoneSyncVCam) return CLASS_E_CLASSNOTAVAILABLE;
    ClassFactory* f = new (std::nothrow) ClassFactory();
    if (!f) return E_OUTOFMEMORY;
    HRESULT hr = f->QueryInterface(riid, ppv);
    f->Release();
    return hr;
}

// Note: a virtual camera source *must* be registered in HKLM so the Frame Server services can load it.
STDAPI DllRegisterServer()
{
    wchar_t path[MAX_PATH];
    GetModuleFileNameW(g_module, path, MAX_PATH);
    HKEY key = nullptr;
    LSTATUS s = RegCreateKeyExW(HKEY_LOCAL_MACHINE, L"Software\\Classes\\CLSID\\" PSVC_CLSID_STRING L"\\InprocServer32",
        0, nullptr, 0, KEY_WRITE, nullptr, &key, nullptr);
    if (s != ERROR_SUCCESS) return HRESULT_FROM_WIN32(s);
    s = RegSetValueExW(key, nullptr, 0, REG_SZ, (const BYTE*)path, (DWORD)((wcslen(path) + 1) * sizeof(wchar_t)));
    if (s == ERROR_SUCCESS)
    {
        const wchar_t tm[] = L"Both";
        s = RegSetValueExW(key, L"ThreadingModel", 0, REG_SZ, (const BYTE*)tm, sizeof(tm));
    }
    RegCloseKey(key);
    if (s == ERROR_SUCCESS)
    {
        HKEY k2;
        if (RegOpenKeyExW(HKEY_LOCAL_MACHINE, L"Software\\Classes\\CLSID\\" PSVC_CLSID_STRING, 0, KEY_WRITE, &k2) == ERROR_SUCCESS)
        {
            const wchar_t name[] = L"PhoneSync Virtual Camera Source";
            RegSetValueExW(k2, nullptr, 0, REG_SZ, (const BYTE*)name, sizeof(name));
            RegCloseKey(k2);
        }
    }
    VLog(L"DllRegisterServer '%s' status=%ld", path, s);
    return HRESULT_FROM_WIN32(s);
}

STDAPI DllUnregisterServer()
{
    LSTATUS s = RegDeleteTreeW(HKEY_LOCAL_MACHINE, L"Software\\Classes\\CLSID\\" PSVC_CLSID_STRING);
    VLog(L"DllUnregisterServer status=%ld", s);
    if (s == ERROR_FILE_NOT_FOUND) s = ERROR_SUCCESS;
    return HRESULT_FROM_WIN32(s);
}
