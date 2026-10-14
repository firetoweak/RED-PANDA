#include <windows.h>
#include <winternl.h>
#include <detours.h>
#include <string>
#include <cstring>
#include "policy.h"

using CreateFn = NTSTATUS(NTAPI*)(PHANDLE,ACCESS_MASK,POBJECT_ATTRIBUTES,PIO_STATUS_BLOCK,PLARGE_INTEGER,ULONG,ULONG,ULONG,ULONG,PVOID,ULONG);
using OpenFn = NTSTATUS(NTAPI*)(PHANDLE,ACCESS_MASK,POBJECT_ATTRIBUTES,PIO_STATUS_BLOCK,ULONG,ULONG);
using WriteFn = NTSTATUS(NTAPI*)(HANDLE,HANDLE,PIO_APC_ROUTINE,PVOID,PIO_STATUS_BLOCK,PVOID,ULONG,PLARGE_INTEGER,PULONG);
using SetFn = NTSTATUS(NTAPI*)(HANDLE,PIO_STATUS_BLOCK,PVOID,ULONG,FILE_INFORMATION_CLASS);
using SectionFn = NTSTATUS(NTAPI*)(PHANDLE,ACCESS_MASK,POBJECT_ATTRIBUTES,PLARGE_INTEGER,ULONG,ULONG,HANDLE);
static CreateFn real_create;
static OpenFn real_open;
static WriteFn real_write;
static SetFn real_set;
static SectionFn real_section;
static auto real_process_w = CreateProcessW;
static auto real_process_a = CreateProcessA;
static Policy policy;
static char hook_path[32768];
static thread_local bool resolving = false;
constexpr NTSTATUS denied = NTSTATUS(0xc0000022);

static bool within(const std::wstring& path) {
    size_t n = wcslen(policy.root);
    return path.size() >= n && CompareStringOrdinal(path.data(), int(n), policy.root, int(n), TRUE) == CSTR_EQUAL &&
        (path.size() == n || path[n] == L'\\' || path[n] == L':');
}
static std::wstring file_identity_path(HANDLE h) {
    auto path = final_path(h);
    if (path.empty() || within(path)) return path;
    FILE_STANDARD_INFO info{};
    if (!GetFileInformationByHandleEx(h,FileStandardInfo,&info,sizeof(info))) return {};
    if (info.NumberOfLinks<=1 || info.Directory) return path;
    // Existing NTFS hard links can name the same host file outside the root.
    // Ask Windows for its names only when the handle actually has multiple links.
    if (path.rfind(L"\\Device\\",0)!=0) return {};
    auto separator=path.find(L'\\',8);
    if (separator==std::wstring::npos) return {};
    auto volume=path.substr(0,separator);
    auto dos=final_path(h,VOLUME_NAME_DOS);
    if (dos.empty()) return {};
    auto stream=dos.find(L':',6);
    if (stream!=std::wstring::npos) dos.resize(stream);
    std::wstring name(32768,L'\0'); DWORD length=DWORD(name.size());
    HANDLE names=FindFirstFileNameW(dos.c_str(),0,&length,name.data());
    if (names==INVALID_HANDLE_VALUE) return {};
    for (;;) {
        if (within(volume+name.c_str())) { FindClose(names); return policy.root; }
        length=DWORD(name.size());
        if (!FindNextFileNameW(names,&length,name.data())) {
            DWORD error=GetLastError(); FindClose(names);
            return error==ERROR_HANDLE_EOF ? path : std::wstring();
        }
    }
}
// Resolve existing objects through handles. For a new object resolve the nearest
// existing parent, preserving RootDirectory for handle-relative native opens.
static std::wstring resolve(POBJECT_ATTRIBUTES attrs, bool parent_only = false) {
    if (!attrs || !attrs->ObjectName) return {};
    auto name = std::wstring(attrs->ObjectName->Buffer, attrs->ObjectName->Length / sizeof(wchar_t));
    if (name.rfind(L"\\Device\\NamedPipe\\", 0) == 0 || name.rfind(L"\\??\\pipe\\", 0) == 0) return L"<pipe>";
    if (name == L"\\Device\\KsecDD") return L"<device>";
    std::wstring suffix;
    for (;;) {
        if (!parent_only) {
            UNICODE_STRING u{USHORT(name.size()*2), USHORT(name.size()*2), name.data()};
            auto a = *attrs; a.ObjectName = &u;
            IO_STATUS_BLOCK io{}; HANDLE h = nullptr;
            auto status = real_create(&h, FILE_READ_ATTRIBUTES, &a, &io, nullptr, 0,
                FILE_SHARE_READ|FILE_SHARE_WRITE|FILE_SHARE_DELETE, FILE_OPEN, 0, nullptr, 0);
            if (status >= 0) {
                if (GetFileType(h)!=FILE_TYPE_DISK) { CloseHandle(h); return L"<device>"; }
                auto path = file_identity_path(h); CloseHandle(h);
                if (!path.empty()) return path + suffix;
                return {};
            }
            if (status != NTSTATUS(0xc0000034) && status != NTSTATUS(0xc000003a) && status != NTSTATUS(0xc000000f)) return {};
            auto colon=name.find_last_of(L':');
            auto slash=name.find_last_of(L'\\');
            if (colon!=std::wstring::npos && (slash==std::wstring::npos || colon>slash)) {
                suffix=name.substr(colon)+suffix;name.resize(colon);continue;
            }
        }
        parent_only = false;
        auto end = name.find_last_of(L'\\');
        if (end == std::wstring::npos) {
            if (attrs->RootDirectory) return final_path(attrs->RootDirectory) + L"\\" + name + suffix;
            return {};
        }
        suffix = name.substr(end) + suffix;
        name.resize(end);
        if (name.empty()) return {};
    }
}
static bool protected_object(POBJECT_ATTRIBUTES attrs) {
    resolving = true;
    auto actual = resolve(attrs);
    auto parent = resolve(attrs, true);
    resolving = false;
    // Unknown filesystem names are refused in this prototype. Named pipes are
    // explicitly allowed; no filesystem operation falls back to an unhooked path.
    return actual.empty() || within(actual) || (!parent.empty() && within(parent));
}
static bool protected_handle(HANDLE file) {
    if (GetFileType(file) != FILE_TYPE_DISK) return false;
    resolving = true; auto path = file_identity_path(file); resolving = false;
    return path.empty() || within(path);
}
static bool writable(ACCESS_MASK access) {
    return (access & (FILE_WRITE_DATA|FILE_APPEND_DATA|FILE_WRITE_EA|FILE_WRITE_ATTRIBUTES|DELETE|WRITE_DAC|WRITE_OWNER|GENERIC_WRITE|GENERIC_ALL|MAXIMUM_ALLOWED)) != 0;
}
static NTSTATUS NTAPI hooked_create(PHANDLE h,ACCESS_MASK access,POBJECT_ATTRIBUTES a,PIO_STATUS_BLOCK io,PLARGE_INTEGER size,ULONG attr,ULONG share,ULONG disposition,ULONG options,PVOID ea,ULONG easize) {
    if (!resolving && (writable(access) || disposition != FILE_OPEN || (options & FILE_DELETE_ON_CLOSE)) && protected_object(a)) return denied;
    return real_create(h,access,a,io,size,attr,share,disposition,options,ea,easize);
}
static NTSTATUS NTAPI hooked_open(PHANDLE h,ACCESS_MASK access,POBJECT_ATTRIBUTES a,PIO_STATUS_BLOCK io,ULONG share,ULONG options) {
    if (!resolving && (writable(access) || (options & FILE_DELETE_ON_CLOSE)) && protected_object(a)) return denied;
    return real_open(h,access,a,io,share,options);
}
static NTSTATUS NTAPI hooked_write(HANDLE h,HANDLE event,PIO_APC_ROUTINE apc,PVOID context,PIO_STATUS_BLOCK io,PVOID data,ULONG len,PLARGE_INTEGER offset,PULONG key) {
    if (!resolving && protected_handle(h)) return denied;
    return real_write(h,event,apc,context,io,data,len,offset,key);
}
static NTSTATUS NTAPI hooked_set(HANDLE h,PIO_STATUS_BLOCK io,PVOID data,ULONG len,FILE_INFORMATION_CLASS kind) {
    int k = int(kind);
    bool rename = k==10 || k==11 || k==55 || k==56 || k==65 || k==66 || k==72 || k==73;
    bool mutation = rename || k==4 || k==13 || k==19 || k==20 || k==39 || k==40 || k==64 || k==71;
    if (!resolving && mutation) {
        if (protected_handle(h)) return denied;
        if (rename && len >= 20) {
            // FILE_RENAME_INFORMATION and FILE_LINK_INFORMATION on x64.
            HANDLE root; ULONG bytes;
            memcpy(&root, (char*)data+8, 8); memcpy(&bytes, (char*)data+16, 4);
            if (bytes > len-20 || bytes > 65534 || (bytes&1)) return denied;
            UNICODE_STRING u{USHORT(bytes),USHORT(bytes),(PWSTR)((char*)data+20)};
            OBJECT_ATTRIBUTES a{}; InitializeObjectAttributes(&a,&u,OBJ_CASE_INSENSITIVE,root,nullptr);
            if (protected_object(&a)) return denied;
        }
    }
    return real_set(h,io,data,len,kind);
}
static NTSTATUS NTAPI hooked_section(PHANDLE section,ACCESS_MASK access,POBJECT_ATTRIBUTES attrs,PLARGE_INTEGER size,ULONG protect,ULONG flags,HANDLE file) {
    if (!resolving && file && (protect & (PAGE_READWRITE|PAGE_EXECUTE_READWRITE)) && protected_handle(file)) return denied;
    return real_section(section,access,attrs,size,protect,flags,file);
}
template<class Create> static BOOL child(Create create, DWORD flags, LPPROCESS_INFORMATION output) {
    if (flags & (CREATE_SUSPENDED|CREATE_BREAKAWAY_FROM_JOB|DEBUG_PROCESS|DEBUG_ONLY_THIS_PROCESS)) { SetLastError(ERROR_NOT_SUPPORTED); return FALSE; }
    Policy p = policy; Gate gate; PROCESS_INFORMATION pi{};
    if (!gate.create(p)) return FALSE;
    if (!create(flags|CREATE_SUSPENDED, &pi)) return FALSE;
    if (!gate.attach(pi,p,hook_path) || !gate.release(pi)) {
        DWORD error = GetLastError(); TerminateProcess(pi.hProcess,126); WaitForSingleObject(pi.hProcess,10000);
        CloseHandle(pi.hThread); CloseHandle(pi.hProcess); SetLastError(error); return FALSE;
    }
    *output = pi; return TRUE;
}
static BOOL WINAPI hooked_process_w(LPCWSTR app,LPWSTR cmd,LPSECURITY_ATTRIBUTES pa,LPSECURITY_ATTRIBUTES ta,BOOL inherit,DWORD flags,LPVOID env,LPCWSTR cwd,LPSTARTUPINFOW si,LPPROCESS_INFORMATION pi) {
    return child([&](DWORD f,PROCESS_INFORMATION* p){return real_process_w(app,cmd,pa,ta,inherit,f,env,cwd,si,p);},flags,pi);
}
static BOOL WINAPI hooked_process_a(LPCSTR app,LPSTR cmd,LPSECURITY_ATTRIBUTES pa,LPSECURITY_ATTRIBUTES ta,BOOL inherit,DWORD flags,LPVOID env,LPCSTR cwd,LPSTARTUPINFOA si,LPPROCESS_INFORMATION pi) {
    return child([&](DWORD f,PROCESS_INFORMATION* p){return real_process_a(app,cmd,pa,ta,inherit,f,env,cwd,si,p);},flags,pi);
}
extern "C" __declspec(dllexport) void HookOrdinalOne() {}
BOOL WINAPI DllMain(HINSTANCE module, DWORD reason, LPVOID) {
    if (reason != DLL_PROCESS_ATTACH) return TRUE;
    DWORD bytes = 0;
    auto p = (Policy*)DetourFindPayloadEx(policy_guid,&bytes);
    if (!p || bytes!=sizeof(Policy) || p->size!=sizeof(Policy) || !p->root[0] || p->root[32767]) return FALSE;
    policy = *p;
    if (!GetModuleFileNameA(module,hook_path,sizeof(hook_path))) return FALSE;
    if (!DetourRestoreAfterWith()) return FALSE;
    auto ntdll = GetModuleHandleW(L"ntdll.dll");
    real_create=(CreateFn)GetProcAddress(ntdll,"NtCreateFile");
    real_open=(OpenFn)GetProcAddress(ntdll,"NtOpenFile");
    real_write=(WriteFn)GetProcAddress(ntdll,"NtWriteFile");
    real_set=(SetFn)GetProcAddress(ntdll,"NtSetInformationFile");
    real_section=(SectionFn)GetProcAddress(ntdll,"NtCreateSection");
    if (DetourTransactionBegin()!=NO_ERROR || DetourUpdateThread(GetCurrentThread())!=NO_ERROR) return FALSE;
#ifdef PROBE_ATTACH_FAILURE
    if (DetourAttach((PVOID*)nullptr,(PVOID)hooked_create)!=NO_ERROR) return FALSE;
#endif
    if (DetourAttach(&real_create,hooked_create)!=NO_ERROR ||
        DetourAttach(&real_open,hooked_open)!=NO_ERROR ||
        DetourAttach(&real_write,hooked_write)!=NO_ERROR ||
        DetourAttach(&real_set,hooked_set)!=NO_ERROR ||
        DetourAttach(&real_section,hooked_section)!=NO_ERROR ||
        DetourAttach(&real_process_w,hooked_process_w)!=NO_ERROR ||
        DetourAttach(&real_process_a,hooked_process_a)!=NO_ERROR ||
        DetourTransactionCommit()!=NO_ERROR) return FALSE;
    HANDLE ready=OpenEventW(EVENT_MODIFY_STATE,FALSE,policy.ready);
    HANDLE go=OpenEventW(SYNCHRONIZE,FALSE,policy.go);
    if (!ready || !go || !SetEvent(ready)) return FALSE;
    DWORD result=WaitForSingleObject(go,10000);
    CloseHandle(ready); CloseHandle(go);
    return result==WAIT_OBJECT_0;
}
