// Simple multi-process file logger: C:\ProgramData\PhoneSync\vcam.log
#include "common.h"
#include <stdio.h>
#include <stdlib.h>
#include <stdarg.h>
#include <sddl.h>

static void EnsureLogDir(const wchar_t* dir)
{
    if (GetFileAttributesW(dir) != INVALID_FILE_ATTRIBUTES)
        return;
    // permissive ACL so Frame Server (LocalService), apps and the user can all write
    SECURITY_ATTRIBUTES sa{ sizeof(sa), nullptr, FALSE };
    PSECURITY_DESCRIPTOR sd = nullptr;
    if (ConvertStringSecurityDescriptorToSecurityDescriptorW(
        L"D:(A;OICI;GA;;;WD)(A;OICI;GA;;;SY)(A;OICI;GA;;;BA)", SDDL_REVISION_1, &sd, nullptr))
        sa.lpSecurityDescriptor = sd;
    CreateDirectoryW(L"C:\\ProgramData\\PhoneSync", &sa);
    CreateDirectoryW(dir, &sa);
    if (sd) LocalFree(sd);
}

void VLog(const wchar_t* fmt, ...)
{
    static const wchar_t* dir = L"C:\\ProgramData\\PhoneSync";
    static const wchar_t* path = L"C:\\ProgramData\\PhoneSync\\vcam.log";
    EnsureLogDir(dir);

    wchar_t msg[1536];
    va_list ap;
    va_start(ap, fmt);
    _vsnwprintf_s(msg, _countof(msg), _TRUNCATE, fmt, ap);
    va_end(ap);

    static wchar_t exe[MAX_PATH] = L"";
    if (!exe[0])
    {
        wchar_t full[MAX_PATH];
        GetModuleFileNameW(nullptr, full, MAX_PATH);
        const wchar_t* p = wcsrchr(full, L'\\');
        wcscpy_s(exe, p ? p + 1 : full);
    }

    SYSTEMTIME st;
    GetLocalTime(&st);
    wchar_t line[2048];
    int n = _snwprintf_s(line, _countof(line), _TRUNCATE, L"%04d-%02d-%02d %02d:%02d:%02d.%03d [%lu %s] %s\r\n",
        st.wYear, st.wMonth, st.wDay, st.wHour, st.wMinute, st.wSecond, st.wMilliseconds,
        GetCurrentProcessId(), exe, msg);
    if (n < 0) n = (int)wcslen(line);

    char utf8[4096];
    int len = WideCharToMultiByte(CP_UTF8, 0, line, n, utf8, sizeof(utf8), nullptr, nullptr);
    if (len <= 0) return;

    HANDLE h = CreateFileW(path, FILE_APPEND_DATA, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        nullptr, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (h == INVALID_HANDLE_VALUE) return;
    LARGE_INTEGER size{};
    if (GetFileSizeEx(h, &size) && size.QuadPart > 8 * 1024 * 1024)
    {
        // rotate: truncate when > 8MB
        CloseHandle(h);
        h = CreateFileW(path, GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            nullptr, TRUNCATE_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
        if (h == INVALID_HANDLE_VALUE) return;
    }
    DWORD written;
    WriteFile(h, utf8, (DWORD)len, &written, nullptr);
    CloseHandle(h);
}

