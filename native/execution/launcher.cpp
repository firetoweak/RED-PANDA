#include "policy.h"
#include <cstdio>
#include <windows.h>

static std::wstring quote(const wchar_t *arg) {
  std::wstring result = L"\"";
  unsigned slashes = 0;
  for (; *arg; ++arg) {
    if (*arg == L'\\') {
      ++slashes;
      continue;
    }
    result.append(*arg == L'"' ? slashes * 2 + 1 : slashes, L'\\');
    result += *arg;
    slashes = 0;
  }
  result.append(slashes * 2, L'\\');
  return result + L'"';
}
static bool send(HANDLE pipe, DWORD kind, DWORD stage, DWORD error,
                 DWORD exit = 0) {
  Status value{1, kind, stage, error, exit};
  DWORD written;
  return WriteFile(pipe, &value, sizeof(value), &written, nullptr) &&
         written == sizeof(value);
}
static bool stop_process(HANDLE process) {
  DWORD error = ERROR_SUCCESS;
  if (!TerminateProcess(process, 130)) {
    error = GetLastError();
    if (error != ERROR_ACCESS_DENIED)
      return false;
  }
  // The process can finish between the readiness check and TerminateProcess.
  // ERROR_ACCESS_DENIED is only accepted when its exit is confirmed.
  DWORD result = WaitForSingleObject(process, 10000);
  if (result == WAIT_OBJECT_0)
    return true;
  if (result == WAIT_TIMEOUT)
    SetLastError(error ? error : ERROR_TIMEOUT);
  return false;
}
static bool reap(HANDLE job) {
  if (!TerminateJobObject(job, 130))
    return false;
  ULONGLONG deadline = GetTickCount64() + 10000;
  for (;;) {
    JOBOBJECT_BASIC_ACCOUNTING_INFORMATION info{};
    if (!QueryInformationJobObject(job, JobObjectBasicAccountingInformation,
                                   &info, sizeof(info), nullptr))
      return false;
    if (!info.ActiveProcesses)
      return true;
    if (GetTickCount64() >= deadline) {
      SetLastError(ERROR_TIMEOUT);
      return false;
    }
    Sleep(10);
  }
}
int wmain(int argc, wchar_t **argv) {
  if (argc < 5)
    return 125;
  HANDLE status = (HANDLE)(ULONG_PTR)_wcstoui64(argv[1], nullptr, 10);
  // Python assigns this launcher to its kill-on-close Job before releasing it.
  char input;
  DWORD read;
  if (!ReadFile(GetStdHandle(STD_INPUT_HANDLE), &input, 1, &read, nullptr) ||
      read != 1)
    return 125;
  Policy p{};
  p.size = sizeof(p);
  HANDLE root = CreateFileW(argv[2], FILE_READ_ATTRIBUTES, 7, nullptr,
                            OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, nullptr);
  if (root == INVALID_HANDLE_VALUE) {
    send(status, 3, Create, GetLastError());
    return 125;
  }
  auto path = final_path(root);
  CloseHandle(root);
  if (path.empty() || path.size() >= 32768) {
    send(status, 3, Create, GetLastError());
    return 125;
  }
  while (path.back() == L'\\')
    path.pop_back();
  wcscpy(p.root, path.c_str());
  wchar_t module[32768];
  DWORD length = GetModuleFileNameW(nullptr, module, 32768);
  if (!length || length >= 32768) {
    send(status, 3, Attach, ERROR_BAD_PATHNAME);
    return 125;
  }
  auto directory =
      std::wstring(module).substr(0, std::wstring(module).find_last_of(L'\\'));
  wcscpy(p.dll_directory, directory.c_str());
  swprintf(p.fault_event, 128, L"Local\\RedPandaFault-%lu-%llu",
           GetCurrentProcessId(), GetTickCount64());
  swprintf(p.fault_mapping, 128, L"%ls-Map", p.fault_event);
  HANDLE fault_event = CreateEventW(nullptr, TRUE, FALSE, p.fault_event);
  HANDLE mapping =
      CreateFileMappingW(INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE, 0,
                         sizeof(Fault), p.fault_mapping);
  auto fault = mapping ? (Fault *)MapViewOfFile(mapping, FILE_MAP_ALL_ACCESS, 0,
                                                0, sizeof(Fault))
                       : nullptr;
  HANDLE job = CreateJobObjectW(nullptr, nullptr);
  JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits{};
  limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
  PROCESS_INFORMATION pi{};
  Gate gate;
  DWORD failure = 0, stage = Create, code = 0;
  if (!fault_event || !fault || !job ||
      !SetInformationJobObject(job, JobObjectExtendedLimitInformation, &limits,
                               sizeof(limits)) ||
      !gate.create(p))
    failure = GetLastError();
  STARTUPINFOEXW si{};
  si.StartupInfo.cb = sizeof(si);
  si.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
  si.StartupInfo.hStdInput = GetStdHandle(STD_INPUT_HANDLE);
  si.StartupInfo.hStdOutput = GetStdHandle(STD_OUTPUT_HANDLE);
  si.StartupInfo.hStdError = GetStdHandle(STD_ERROR_HANDLE);
  HANDLE handles[] = {si.StartupInfo.hStdInput, si.StartupInfo.hStdOutput,
                      si.StartupInfo.hStdError};
  SIZE_T bytes = 0;
  InitializeProcThreadAttributeList(nullptr, 1, 0, &bytes);
  si.lpAttributeList =
      (LPPROC_THREAD_ATTRIBUTE_LIST)HeapAlloc(GetProcessHeap(), 0, bytes);
  bool attributes = false;
  if (!failure) {
    attributes = si.lpAttributeList && InitializeProcThreadAttributeList(
                                           si.lpAttributeList, 1, 0, &bytes);
    if (!attributes ||
        !UpdateProcThreadAttribute(si.lpAttributeList, 0,
                                   PROC_THREAD_ATTRIBUTE_HANDLE_LIST, handles,
                                   sizeof(handles), nullptr, nullptr))
      failure = GetLastError();
  }
  std::wstring command;
  for (int i = 4; i < argc; ++i) {
    if (i > 4)
      command += L' ';
    command += quote(argv[i]);
  }
  if (!failure &&
      !CreateProcessW(argv[4], command.data(), nullptr, nullptr, TRUE,
                      CREATE_SUSPENDED | CREATE_NO_WINDOW |
                          EXTENDED_STARTUPINFO_PRESENT,
                      nullptr, argv[3], &si.StartupInfo, &pi))
    failure = GetLastError();
  if (attributes)
    DeleteProcThreadAttributeList(si.lpAttributeList);
  if (si.lpAttributeList)
    HeapFree(GetProcessHeap(), 0, si.lpAttributeList);
  if (!failure) {
    stage = Attach;
    if (!AssignProcessToJobObject(job, pi.hProcess) || !gate.attach(pi, p) ||
        !gate.ready_to_release(pi))
      failure = GetLastError();
  }
  if (!failure) {
    if (!send(status, 1, 0, 0) || !SetEvent(gate.go)) {
      failure = GetLastError();
      stage = Protocol;
    } else {
      HANDLE wait[] = {pi.hProcess, fault_event};
      DWORD result = WaitForMultipleObjects(2, wait, FALSE, INFINITE);
      if (result == WAIT_FAILED) {
        failure = GetLastError();
        stage = Protocol;
      } else if (result == WAIT_OBJECT_0 + 1) {
        failure = fault->error;
        stage = fault->stage;
      } else if (!GetExitCodeProcess(pi.hProcess, &code)) {
        failure = GetLastError();
        stage = Protocol;
      }
    }
  }
  // Failed assignment can leave a suspended process outside the inner Job.
  if (pi.hProcess) {
    DWORD state = WaitForSingleObject(pi.hProcess, 0);
    if (state == WAIT_FAILED ||
        (state == WAIT_TIMEOUT && !stop_process(pi.hProcess))) {
      failure = GetLastError();
      if (!failure)
        failure = ERROR_TIMEOUT;
      stage = Reap;
    }
  }
  if (job && !reap(job)) {
    failure = GetLastError();
    stage = Reap;
  }
  if (stage != Reap && stage != Protocol && fault && fault->claimed == 2) {
    failure = fault->error;
    stage = fault->stage;
  } else if (stage != Reap && stage != Protocol && fault && fault->claimed &&
             fault->claimed != 2) {
    failure = ERROR_INVALID_DATA;
    stage = Protocol;
  }
  send(status, failure ? 3 : 2, stage, failure, code);
  if (pi.hThread)
    CloseHandle(pi.hThread);
  if (pi.hProcess)
    CloseHandle(pi.hProcess);
  if (job)
    CloseHandle(job);
  if (fault)
    UnmapViewOfFile(fault);
  if (mapping)
    CloseHandle(mapping);
  if (fault_event)
    CloseHandle(fault_event);
  CloseHandle(status);
  return failure ? 125 : 0;
}
