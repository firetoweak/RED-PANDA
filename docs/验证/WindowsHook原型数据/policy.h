#pragma once
#include <windows.h>
#include <detours.h>
#include <string>

inline const GUID policy_guid = {0x36b51bf6,0xb492,0x4cf1,{0x94,0x7d,0x4b,0x21,0x32,0x7e,0x2a,0x61}};
struct Policy {
    DWORD size;
    wchar_t root[32768];
    wchar_t ready[128];
    wchar_t go[128];
};
inline std::wstring final_path(HANDLE file, DWORD volume = VOLUME_NAME_NT) {
    DWORD n = GetFinalPathNameByHandleW(file, nullptr, 0, volume);
    if (!n) return {};
    std::wstring result(n, L'\0');
    DWORD used = GetFinalPathNameByHandleW(file, result.data(), n, volume);
    if (!used || used >= n) return {};
    result.resize(used);
    return result;
}
struct Gate {
    HANDLE ready = nullptr, go = nullptr;
    ~Gate() { if (ready) CloseHandle(ready); if (go) CloseHandle(go); }
    bool create(Policy& p) {
        static LONG serial = 0;
        auto value = InterlockedIncrement(&serial);
        swprintf(p.ready, 128, L"Local\\RedPandaHookReady-%lu-%llu-%ld", GetCurrentProcessId(), GetTickCount64(), value);
        swprintf(p.go, 128, L"%ls-Go", p.ready);
        ready = CreateEventW(nullptr, TRUE, FALSE, p.ready);
        go = CreateEventW(nullptr, TRUE, FALSE, p.go);
        return ready && go;
    }
    bool attach(PROCESS_INFORMATION& pi, Policy& p, const char* dll) {
        USHORT machine, native;
        if (!IsWow64Process2(pi.hProcess, &machine, &native) || machine != IMAGE_FILE_MACHINE_UNKNOWN || native != IMAGE_FILE_MACHINE_AMD64) {
            SetLastError(ERROR_NOT_SUPPORTED); return false;
        }
#ifndef PROBE_NO_POLICY
        if (!DetourCopyPayloadToProcess(pi.hProcess, policy_guid, &p, sizeof(p))) return false;
#endif
        if (!DetourUpdateProcessWithDll(pi.hProcess, &dll, 1)) return false;
        return true;
    }
    bool release(PROCESS_INFORMATION& pi) {
        if (ResumeThread(pi.hThread) == DWORD(-1)) return false;
        HANDLE handles[] = {ready, pi.hProcess};
        if (WaitForMultipleObjects(2, handles, FALSE, 10000) != WAIT_OBJECT_0) {
            SetLastError(ERROR_DLL_INIT_FAILED); return false;
        }
        return SetEvent(go);
    }
};
