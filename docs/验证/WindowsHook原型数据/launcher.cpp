#include <windows.h>
#include <cstdio>
#include <string>
#include "policy.h"

static std::wstring quote(const wchar_t* arg) {
    std::wstring result=L"\""; unsigned slashes=0;
    for (;*arg;++arg) {
        if (*arg==L'\\') {++slashes;continue;}
        if (*arg==L'"') result.append(slashes*2+1,L'\\');
        else result.append(slashes,L'\\');
        result+=*arg; slashes=0;
    }
    result.append(slashes*2,L'\\'); return result+L'"';
}
int wmain(int argc,wchar_t** argv) {
    if (argc<5) { fwprintf(stderr,L"launcher HOST_ROOT DLL CWD EXE [args...]\n"); return 125; }
    Policy p{}; p.size=sizeof(p);
    HANDLE root=CreateFileW(argv[1],FILE_READ_ATTRIBUTES,FILE_SHARE_READ|FILE_SHARE_WRITE|FILE_SHARE_DELETE,nullptr,OPEN_EXISTING,FILE_FLAG_BACKUP_SEMANTICS,nullptr);
    if (root==INVALID_HANDLE_VALUE) return 125;
    auto path=final_path(root); CloseHandle(root);
    if (path.empty() || path.size()>=32768) return 125;
    wcscpy(p.root,path.c_str());
    char dll[32768];
    if (!WideCharToMultiByte(CP_UTF8,WC_ERR_INVALID_CHARS,argv[2],-1,dll,sizeof(dll),nullptr,nullptr)) return 125;
    std::wstring cmd;
    for (int i=4;i<argc;++i) {if(i>4)cmd+=L' ';cmd+=quote(argv[i]);}
    HANDLE job=CreateJobObjectW(nullptr,nullptr);
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits{};
    limits.BasicLimitInformation.LimitFlags=JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
    if (!job || !SetInformationJobObject(job,JobObjectExtendedLimitInformation,&limits,sizeof(limits))) return 125;
    Gate gate; PROCESS_INFORMATION pi{}; STARTUPINFOW si{sizeof(si)};
    si.dwFlags=STARTF_USESTDHANDLES; si.hStdInput=GetStdHandle(STD_INPUT_HANDLE); si.hStdOutput=GetStdHandle(STD_OUTPUT_HANDLE);si.hStdError=GetStdHandle(STD_ERROR_HANDLE);
    if (!gate.create(p) || !CreateProcessW(argv[4],cmd.data(),nullptr,nullptr,TRUE,CREATE_SUSPENDED|CREATE_NO_WINDOW,nullptr,argv[3],&si,&pi)) {
        fprintf(stderr,"create failed: %lu\n",GetLastError());CloseHandle(job); return 125;
    }
    if (!AssignProcessToJobObject(job,pi.hProcess) || !gate.attach(pi,p,dll) || !gate.release(pi)) {
        fprintf(stderr,"hook attachment failed: %lu\n",GetLastError());
        TerminateProcess(pi.hProcess,126);WaitForSingleObject(pi.hProcess,10000);
        CloseHandle(pi.hThread);CloseHandle(pi.hProcess);CloseHandle(job);return 126;
    }
    WaitForSingleObject(pi.hProcess,INFINITE);DWORD code;GetExitCodeProcess(pi.hProcess,&code);
    // A task may leave grandchildren running. Reap the Job before returning so
    // the owner can safely finish and release its file view.
    if (!TerminateJobObject(job,127)) code=125;
    ULONGLONG deadline=GetTickCount64()+10000;
    for (;;) {
        JOBOBJECT_BASIC_ACCOUNTING_INFORMATION accounting{};
        if (!QueryInformationJobObject(job,JobObjectBasicAccountingInformation,&accounting,sizeof(accounting),nullptr)) {code=125;break;}
        if (!accounting.ActiveProcesses) break;
        if (GetTickCount64()>=deadline) {code=125;break;}
        Sleep(10);
    }
    CloseHandle(pi.hThread);CloseHandle(pi.hProcess);CloseHandle(job);return int(code);
}
