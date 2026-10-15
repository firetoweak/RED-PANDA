// Drive the production launcher at its go boundary, without product test flags.
#include "policy.h"
#include <cstdio>

static HANDLE target_process;
static HANDLE race_gate;
static BOOL WINAPI probe_create_process(
    LPCWSTR app, LPWSTR command, LPSECURITY_ATTRIBUTES process_attributes,
    LPSECURITY_ATTRIBUTES thread_attributes, BOOL inherit, DWORD flags,
    LPVOID environment, LPCWSTR cwd, LPSTARTUPINFOW startup,
    LPPROCESS_INFORMATION output) {
  wchar_t marker[32768];
  GetEnvironmentVariableW(L"REDPANDA_GATE_EXPECTED_MARKER", marker, 32768);
  wchar_t mode[32];
  GetEnvironmentVariableW(L"REDPANDA_GATE_CASE", mode, 32);
  if (!wcscmp(mode, L"fault-exit-race") || !wcscmp(mode, L"access-denied-live")) {
    wchar_t name[128];
    swprintf(name,128,L"Local\\GateExitRace-%lu",GetCurrentProcessId());
    race_gate=CreateEventW(nullptr,TRUE,FALSE,name);
    SetEnvironmentVariableW(L"REDPANDA_GATE_RACE_EVENT",name);
  }
  SetEnvironmentVariableW(L"REDPANDA_GATE_TARGET_MARKER", marker);
  BOOL created = CreateProcessW(app, command, process_attributes,
                               thread_attributes, inherit, flags, environment,
                               cwd, startup, output);
  DWORD error = GetLastError();
  SetEnvironmentVariableW(L"REDPANDA_GATE_TARGET_MARKER", nullptr);
  if (created)
    target_process = output->hProcess;
  SetLastError(error);
  return created;
}

static BOOL WINAPI probe_set_event(HANDLE event) {
  wchar_t mode[32];
  GetEnvironmentVariableW(L"REDPANDA_GATE_CASE", mode, 32);
  if (!wcscmp(mode, L"short-delay"))
    Sleep(500);
  if (!wcscmp(mode, L"withhold") || !wcscmp(mode, L"fault-exit-race") ||
      !wcscmp(mode, L"access-denied-live"))
    return TRUE;
  if (!wcscmp(mode, L"late-go")) {
    // Wait for both process exit and its previously published Fault. The real
    // launcher's process-first wait must still preserve that Fault at cleanup.
    if (WaitForSingleObject(target_process, 15000) != WAIT_OBJECT_0) {
      SetLastError(ERROR_TIMEOUT);
      return FALSE;
    }
  }
  return SetEvent(event);
}

static BOOL WINAPI probe_terminate_process(HANDLE process, UINT exit_code) {
  wchar_t mode[32];
  GetEnvironmentVariableW(L"REDPANDA_GATE_CASE", mode, 32);
  if (!wcscmp(mode, L"access-denied-live") && process == target_process) {
    HANDLE restricted;
    if (!DuplicateHandle(GetCurrentProcess(), process, GetCurrentProcess(),
                         &restricted, SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION,
                         FALSE, 0))
      return FALSE;
    BOOL result = TerminateProcess(restricted, exit_code);
    DWORD error = GetLastError(), code = 0;
    DWORD state = WaitForSingleObject(restricted, 0);
    GetExitCodeProcess(restricted, &code);
    fprintf(stderr, "terminate_error=%lu wait=%lu exit=%lu\n", error, state, code);
    CloseHandle(restricted);
    SetLastError(error);
    return result;
  }
  if (race_gate && process == target_process) {
    // The hook has published its fault and is waiting at the test-only barrier.
    // Let it exit between the launcher's zero-time check and TerminateProcess.
    SetEvent(race_gate);
    WaitForSingleObject(process,15000);
  }
  BOOL result=TerminateProcess(process,exit_code);
  DWORD error=GetLastError();
  if (!result) {
    DWORD state=WaitForSingleObject(process,0),code=0;
    GetExitCodeProcess(process,&code);
    fprintf(stderr,"terminate_error=%lu wait=%lu exit=%lu\n",error,state,code);
  }
  SetLastError(error);
  return result;
}

static BOOL WINAPI probe_terminate_job(HANDLE job, UINT exit_code) {
  JOBOBJECT_BASIC_ACCOUNTING_INFORMATION info{};
  if (QueryInformationJobObject(job, JobObjectBasicAccountingInformation,
                               &info, sizeof(info), nullptr))
    fprintf(stderr, "reap_job_active_before=%lu\n", info.ActiveProcesses);
  return TerminateJobObject(job, exit_code);
}

static BOOL WINAPI probe_query_job(HANDLE job, JOBOBJECTINFOCLASS kind,
                                   LPVOID info, DWORD size, LPDWORD returned) {
  BOOL result = QueryInformationJobObject(job, kind, info, size, returned);
  if (result && kind == JobObjectBasicAccountingInformation)
    fprintf(stderr, "reap_job_active=%lu\n",
            ((JOBOBJECT_BASIC_ACCOUNTING_INFORMATION *)info)->ActiveProcesses);
  return result;
}

#define CreateProcessW probe_create_process
#define SetEvent probe_set_event
#define TerminateProcess probe_terminate_process
#define TerminateJobObject probe_terminate_job
#define QueryInformationJobObject probe_query_job
#define wmain production_launcher_main
#include "../../native/execution/launcher.cpp"
#undef wmain
#undef SetEvent
#undef TerminateProcess
#undef TerminateJobObject
#undef QueryInformationJobObject
#undef CreateProcessW

static void NTAPI user_tls(PVOID, DWORD reason, PVOID) {
  if (reason != DLL_PROCESS_ATTACH)
    return;
  wchar_t marker[32768];
  if (!GetEnvironmentVariableW(L"REDPANDA_GATE_TARGET_MARKER", marker, 32768))
    return;
  HANDLE file = CreateFileW(marker, GENERIC_WRITE, 7, nullptr, CREATE_ALWAYS,
                            FILE_ATTRIBUTE_NORMAL, nullptr);
  if (file != INVALID_HANDLE_VALUE) {
    DWORD written;
    WriteFile(file, "entered", 7, &written, nullptr);
    CloseHandle(file);
  }
}
extern "C" __attribute__((section(".CRT$XLB"), used))
PIMAGE_TLS_CALLBACK gate_user_tls = user_tls;

int wmain(int argc, wchar_t **argv) {
  if (argc == 2 && !wcscmp(argv[1], L"--descendant")) {
    wchar_t name[128];
    GetEnvironmentVariableW(L"REDPANDA_GATE_CHILD_READY", name, 128);
    HANDLE ready = OpenEventW(EVENT_MODIFY_STATE, FALSE, name);
    if (!ready || !SetEvent(ready))
      return 9;
    CloseHandle(ready);
    Sleep(60000);
    return 9;
  }
  if (argc == 2 && !wcscmp(argv[1], L"--target")) {
    wchar_t mode[32];
    GetEnvironmentVariableW(L"REDPANDA_GATE_CASE", mode, 32);
    if (!wcscmp(mode, L"job-child")) {
      wchar_t name[128], module[32768];
      swprintf(name, 128, L"Local\\GateChild-%lu", GetCurrentProcessId());
      HANDLE ready = CreateEventW(nullptr, TRUE, FALSE, name);
      SetEnvironmentVariableW(L"REDPANDA_GATE_CHILD_READY", name);
      GetModuleFileNameW(nullptr, module, 32768);
      std::wstring command = quote(module) + L" --descendant";
      STARTUPINFOW startup{};
      startup.cb = sizeof(startup);
      PROCESS_INFORMATION child{};
      if (!CreateProcessW(module, command.data(), nullptr, nullptr, FALSE, 0,
                          nullptr, nullptr, &startup, &child))
        return 9;
      DWORD result = WaitForSingleObject(ready, 10000);
      CloseHandle(child.hThread);
      CloseHandle(child.hProcess);
      CloseHandle(ready);
      return result == WAIT_OBJECT_0 ? 0 : 9;
    }
    return 0;
  }
  return production_launcher_main(argc, argv);
}
