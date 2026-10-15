// Hold fault publication at a test-only barrier to make the exit race repeatable.
#include "policy.h"
static BOOL WINAPI probe_fault_event(HANDLE);
#define SetEvent probe_fault_event
#include "../../native/execution/hook.cpp"
#undef SetEvent

static BOOL WINAPI probe_fault_event(HANDLE event) {
  BOOL result=SetEvent(event);
  if (fault && fault->claimed==2) {
    wchar_t name[128];
    if (GetEnvironmentVariableW(L"REDPANDA_GATE_RACE_EVENT",name,128)) {
      HANDLE release=OpenEventW(SYNCHRONIZE,FALSE,name);
      if (release) {
        wchar_t mode[32];
        GetEnvironmentVariableW(L"REDPANDA_GATE_CASE", mode, 32);
        WaitForSingleObject(release, !wcscmp(mode, L"access-denied-live") ? 30000 : 5000);
        CloseHandle(release);
      }
    }
  }
  return result;
}
