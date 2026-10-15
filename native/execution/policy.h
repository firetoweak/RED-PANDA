#pragma once
#include <windows.h>
#include <detours.h>
#include <string>

inline const GUID policy_guid = {
    0x36b51bf6,
    0xb492,
    0x4cf1,
    {0x94, 0x7d, 0x4b, 0x21, 0x32, 0x7e, 0x2a, 0x61}};
enum Stage : DWORD { Create = 1, Attach, Child, Resolve, Reap, Protocol };
struct Fault {
  volatile LONG claimed;
  DWORD stage;
  DWORD error;
  DWORD pid;
};
struct Policy {
  DWORD size;
  wchar_t root[32768];
  wchar_t ready[128];
  wchar_t go[128];
  wchar_t fault_event[128];
  wchar_t fault_mapping[128];
  wchar_t dll_directory[32768];
};
struct Status {
  DWORD version = 1;
  DWORD kind;
  DWORD stage;
  DWORD error;
  DWORD exit_code;
};
inline std::wstring final_path(HANDLE file, DWORD volume = VOLUME_NAME_NT) {
  DWORD n = GetFinalPathNameByHandleW(file, nullptr, 0, volume);
  if (!n)
    return {};
  std::wstring path(n, L'\0');
  DWORD used = GetFinalPathNameByHandleW(file, path.data(), n, volume);
  if (!used || used >= n) {
    if (used >= n)
      SetLastError(ERROR_MORE_DATA);
    return {};
  }
  path.resize(used);
  return path;
}
struct Gate {
  HANDLE ready = nullptr, go = nullptr;
  ~Gate() {
    if (ready)
      CloseHandle(ready);
    if (go)
      CloseHandle(go);
  }
  bool create(Policy &p) {
    static LONG serial = 0;
    swprintf(p.ready, 128, L"Local\\RedPandaReady-%lu-%llu-%ld",
             GetCurrentProcessId(), GetTickCount64(),
             InterlockedIncrement(&serial));
    swprintf(p.go, 128, L"%ls-Go", p.ready);
    ready = CreateEventW(nullptr, TRUE, FALSE, p.ready);
    go = CreateEventW(nullptr, TRUE, FALSE, p.go);
    return ready && go;
  }
  bool attach(PROCESS_INFORMATION &pi, Policy &p) {
    USHORT machine, native;
    if (!IsWow64Process2(pi.hProcess, &machine, &native))
      return false;
    if (machine != IMAGE_FILE_MACHINE_UNKNOWN ||
        native != IMAGE_FILE_MACHINE_AMD64) {
      SetLastError(ERROR_NOT_SUPPORTED);
      return false;
    }
    std::wstring filename =
        std::wstring(p.dll_directory) + L"\\redpanda-hook.dll";
    char encoded[32768];
    BOOL replaced = FALSE;
    UINT codepage = GetACP();
    auto encode = [&]() {
      replaced = FALSE;
      return WideCharToMultiByte(
                 codepage, codepage == CP_UTF8 ? 0 : WC_NO_BEST_FIT_CHARS,
                 filename.c_str(), -1, encoded, sizeof(encoded), nullptr,
                 codepage == CP_UTF8 ? nullptr : &replaced) &&
             !replaced;
    };
    if (!encode()) {
      wchar_t shortname[32768];
      DWORD n = GetShortPathNameW(filename.c_str(), shortname, 32768);
      if (!n || n >= 32768) {
        SetLastError(ERROR_NO_UNICODE_TRANSLATION);
        return false;
      }
      filename = shortname;
      if (!encode()) {
        SetLastError(ERROR_NO_UNICODE_TRANSLATION);
        return false;
      }
    }
    const char *dll = encoded;
    return DetourCopyPayloadToProcess(pi.hProcess, policy_guid, &p,
                                      sizeof(p)) &&
           DetourUpdateProcessWithDll(pi.hProcess, &dll, 1);
  }
  bool ready_to_release(PROCESS_INFORMATION &pi) {
    if (ResumeThread(pi.hThread) == DWORD(-1))
      return false;
    HANDLE handles[] = {ready, pi.hProcess};
    DWORD result = WaitForMultipleObjects(2, handles, FALSE, 10000);
    if (result != WAIT_OBJECT_0) {
      if (result != WAIT_FAILED)
        SetLastError(ERROR_DLL_INIT_FAILED);
      return false;
    }
    return true;
  }
};
