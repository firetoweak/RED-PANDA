/* WinFsp ABI bridge. Filesystem operations are dispatched through FileSystem. */
#include <windows.h>
#include <assert.h>
#undef _ReadWriteBarrier
#include <winfsp/winfsp.h>
#include <sddl.h>
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <wchar.h>
#include <string.h>
_Static_assert(sizeof(FSP_FSCTL_VOLUME_PARAMS)==504,"WinFsp volume ABI");
_Static_assert(sizeof(FSP_FSCTL_FILE_INFO)==72,"WinFsp file ABI");
typedef NTSTATUS (*VFS_CALLBACK)(void *,uint32_t,uint64_t,uint64_t,uint32_t,const void *,void **,uint32_t *);
typedef struct { VFS_CALLBACK callback;void *context;PSECURITY_DESCRIPTOR security;DWORD security_size;BOOL case_sensitive; } BRIDGE;
typedef struct {BOOL root,cleaned;uint64_t node;WCHAR name[1024];PVOID directory_buffer;} MOUNT_CONTEXT;
void *vfs_bridge_alloc(size_t len) { return malloc(len ? len : 1); }
static NTSTATUS rpc(FSP_FILE_SYSTEM *fs,uint32_t op,uint64_t node,uint64_t offset,uint32_t length,const void *input,void **output,uint32_t *outlen) {
    BRIDGE *bridge=fs->UserContext;
    return bridge->callback(bridge->context,op,node,offset,length,input,output,outlen);
}
static int kind(PWSTR n) {return !wcscmp(n,L"\\")?1:2;}
static int flat(PWSTR n,char *out) {
    if(!n||n[0]!=L'\\'||wcslen(n)>=1024)return 0;
    int length=WideCharToMultiByte(CP_UTF8,WC_ERR_INVALID_CHARS,n+1,-1,out,4096,0,0);
    if(!length)return 0;for(int i=0;i<length;i++)if(out[i]=='\\')out[i]='/';return length-1;
}
static NTSTATUS decoded(BOOL root,const void *data,uint32_t len,FSP_FSCTL_FILE_INFO *fi) {
    memset(fi,0,sizeof *fi);fi->FileAttributes=root?FILE_ATTRIBUTE_DIRECTORY:FILE_ATTRIBUTE_NORMAL;
    if(root) {fi->IndexNumber=1;return STATUS_SUCCESS;}
    if(len!=28&&len!=36)return STATUS_INTERNAL_ERROR;
    memcpy(&fi->IndexNumber,data,8);memcpy(&fi->FileSize,(char*)data+8,8);memcpy(&fi->LastWriteTime,(char*)data+16,8);
    memcpy(&fi->FileAttributes,(char*)data+24,4);
    fi->AllocationSize=(fi->FileSize+4095)&~(UINT64)4095;fi->CreationTime=fi->LastAccessTime=fi->ChangeTime=fi->LastWriteTime;
    return STATUS_SUCCESS;
}
static NTSTATUS named(FSP_FILE_SYSTEM *fs,PWSTR name,uint32_t op,uint64_t flags,uint64_t *handle,FSP_FSCTL_FILE_INFO *fi) {
    if(kind(name)==1)return decoded(TRUE,0,0,fi);
    char path[4096];int len=flat(name,path);if(!len)return STATUS_OBJECT_NAME_NOT_FOUND;
    void *out=0;uint32_t n=0;NTSTATUS st=rpc(fs,op,0,flags,len,path,&out,&n);
    if(NT_SUCCESS(st)&&handle) {
        if(n<=36)st=STATUS_INTERNAL_ERROR;
        else {
            st=decoded(FALSE,out,36,fi);memcpy(handle,(char*)out+28,8);
            if(NT_SUCCESS(st)&&!((BRIDGE*)fs->UserContext)->case_sensitive) {
                FSP_FSCTL_OPEN_FILE_INFO *opened=FspFileSystemGetOpenFileInfo(fi);
                int wide=MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,(char*)out+36,n-36,0,0);
                if(!wide||(wide+1)*sizeof(WCHAR)>opened->NormalizedNameSize)st=STATUS_INTERNAL_ERROR;
                else {
                    opened->NormalizedName[0]=L'\\';
                    MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,(char*)out+36,n-36,opened->NormalizedName+1,wide);
                    for(int i=1;i<=wide;i++)if(opened->NormalizedName[i]==L'/')opened->NormalizedName[i]=L'\\';
                    opened->NormalizedNameSize=(wide+1)*sizeof(WCHAR);
                }
            }
        }
    } else if(NT_SUCCESS(st))st=decoded(FALSE,out,n,fi);
    free(out);return st;
}
static NTSTATUS get_info(FSP_FILE_SYSTEM *fs,PVOID ctx,FSP_FSCTL_FILE_INFO *fi) {
    MOUNT_CONTEXT *c=ctx;if(c->node==1)return decoded(TRUE,0,0,fi);
    void *out=0;uint32_t n=0;NTSTATUS st=rpc(fs,3,c->node,0,0,0,&out,&n);
    if(NT_SUCCESS(st))st=decoded(FALSE,out,n,fi);free(out);return st;
}
static NTSTATUS volume(FSP_FILE_SYSTEM *fs,FSP_FSCTL_VOLUME_INFO *vi) {
    memset(vi,0,sizeof *vi);vi->TotalSize=(UINT64)1<<40;vi->FreeSize=vi->TotalSize;return STATUS_SUCCESS;
}
static NTSTATUS security_copy(FSP_FILE_SYSTEM *fs,PSECURITY_DESCRIPTOR sd,SIZE_T *size) {
    BRIDGE *bridge=fs->UserContext;
    if(size) {SIZE_T cap=*size;*size=bridge->security_size;if(cap<bridge->security_size)return STATUS_BUFFER_OVERFLOW;if(sd)memcpy(sd,bridge->security,bridge->security_size);}return STATUS_SUCCESS;
}
static NTSTATUS security_name(FSP_FILE_SYSTEM *fs,PWSTR name,PUINT32 attr,PSECURITY_DESCRIPTOR sd,SIZE_T *size) {
    FSP_FSCTL_FILE_INFO fi;NTSTATUS st=named(fs,name,6,0,0,&fi);if(!NT_SUCCESS(st))return st;
    if(attr)*attr=fi.FileAttributes;return security_copy(fs,sd,size);
}
static NTSTATUS security_get(FSP_FILE_SYSTEM *fs,PVOID ctx,PSECURITY_DESCRIPTOR sd,SIZE_T *size) {return security_copy(fs,sd,size);}
static VOID cleanup(FSP_FILE_SYSTEM *fs,PVOID ctx,PWSTR name,ULONG flags) {
    MOUNT_CONTEXT *c=ctx;if(c->node==1||c->cleaned)return;
    char path[4096];int length=(flags&FspCleanupDelete)?flat(name?name:c->name,path):0;
    void *out=0;uint32_t n=0;NTSTATUS st=rpc(fs,5,c->node,(flags&FspCleanupDelete)!=0,length,length?path:0,&out,&n);
    free(out);c->cleaned=TRUE;(void)st; /* Rust retains cleanup errors for unmount. */
}
static VOID close_file(FSP_FILE_SYSTEM *fs,PVOID ctx) {MOUNT_CONTEXT *c=ctx;cleanup(fs,ctx,0,0);if(c->node!=1){void *out=0;uint32_t n=0;rpc(fs,15,c->node,0,0,0,&out,&n);free(out);}FspFileSystemDeleteDirectoryBuffer(&c->directory_buffer);free(ctx);}
static NTSTATUS open_file(FSP_FILE_SYSTEM *fs,PWSTR name,UINT32 options,UINT32 access,PVOID *ctx,FSP_FSCTL_FILE_INFO *fi) {
    MOUNT_CONTEXT *c=calloc(1,sizeof *c);if(!c)return STATUS_INSUFFICIENT_RESOURCES;
    c->root=kind(name)==1;NTSTATUS st=named(fs,name,4,(access&(FILE_WRITE_DATA|FILE_APPEND_DATA))!=0,&c->node,fi);if(!NT_SUCCESS(st)) {free(c);return st;}
    if(c->root)c->node=1;c->root=(fi->FileAttributes&FILE_ATTRIBUTE_DIRECTORY)!=0;wcscpy(c->name,name);
    if(options&FILE_DELETE_ON_CLOSE) {
        void *out=0;uint32_t n=0;st=rpc(fs,11,c->node,1,0,0,&out,&n);free(out);
        if(!NT_SUCCESS(st)) {cleanup(fs,c,0,0);free(c);return st;}
    }*ctx=c;return STATUS_SUCCESS;
}
static NTSTATUS create_file(FSP_FILE_SYSTEM *fs,PWSTR name,UINT32 options,UINT32 access,UINT32 attributes,PSECURITY_DESCRIPTOR sd,UINT64 allocation,PVOID *ctx,FSP_FSCTL_FILE_INFO *fi) {
    if(options&FILE_DELETE_ON_CLOSE)return STATUS_NOT_SUPPORTED;
    if(kind(name)!=2)return STATUS_NOT_SUPPORTED;
    MOUNT_CONTEXT *c=calloc(1,sizeof *c);if(!c)return STATUS_INSUFFICIENT_RESOURCES;
    NTSTATUS st=named(fs,name,7,(options&FILE_DIRECTORY_FILE)!=0,&c->node,fi);if(!NT_SUCCESS(st)) {free(c);return st;}
    if(c->root)c->node=1;c->root=(fi->FileAttributes&FILE_ATTRIBUTE_DIRECTORY)!=0;wcscpy(c->name,name);*ctx=c;return STATUS_SUCCESS;
}
static NTSTATUS resize_rpc(FSP_FILE_SYSTEM *fs,MOUNT_CONTEXT *c,uint64_t size,FSP_FSCTL_FILE_INFO *fi) {
    if(c->root)return STATUS_FILE_IS_A_DIRECTORY;
    void *out=0;uint32_t n=0;NTSTATUS st=rpc(fs,9,c->node,size,0,0,&out,&n);
    if(NT_SUCCESS(st))st=decoded(FALSE,out,n,fi);free(out);return st;
}
static NTSTATUS overwrite_file(FSP_FILE_SYSTEM *fs,PVOID ctx,UINT32 attributes,BOOLEAN replace,UINT64 allocation,FSP_FSCTL_FILE_INFO *fi) {
    return resize_rpc(fs,ctx,0,fi);
}
static NTSTATUS read_file(FSP_FILE_SYSTEM *fs,PVOID ctx,PVOID buffer,UINT64 offset,ULONG len,PULONG bytes) {
    MOUNT_CONTEXT *c=ctx;*bytes=0;if(c->root)return STATUS_FILE_IS_A_DIRECTORY;
    void *out=0;uint32_t n=0;NTSTATUS st=rpc(fs,1,c->node,offset,len,0,&out,&n);
    if(NT_SUCCESS(st)) {if(n>len)st=STATUS_INTERNAL_ERROR;else {memcpy(buffer,out,n);*bytes=n;if(!n)st=STATUS_END_OF_FILE;}}
    free(out);return st;
}
static NTSTATUS write_file(FSP_FILE_SYSTEM *fs,PVOID ctx,PVOID buffer,UINT64 offset,ULONG len,BOOLEAN eof,BOOLEAN constrained,PULONG bytes,FSP_FSCTL_FILE_INFO *fi) {
    MOUNT_CONTEXT *c=ctx;*bytes=0;if(c->root)return STATUS_FILE_IS_A_DIRECTORY;
    NTSTATUS st=get_info(fs,ctx,fi);if(!NT_SUCCESS(st))return st;if(eof)offset=fi->FileSize;
    if(constrained) {if(offset>=fi->FileSize)return STATUS_SUCCESS;if(len>fi->FileSize-offset)len=(ULONG)(fi->FileSize-offset);}
    if(!len)return STATUS_SUCCESS;
    void *out=0;uint32_t n=0;st=rpc(fs,2,c->node,offset,len,buffer,&out,&n);
    if(NT_SUCCESS(st)) {st=decoded(FALSE,out,n,fi);if(NT_SUCCESS(st))*bytes=len;}free(out);return st;
}
static NTSTATUS flush_file(FSP_FILE_SYSTEM *fs,PVOID ctx,FSP_FSCTL_FILE_INFO *fi) {
    void *out=0;uint32_t n=0;NTSTATUS st=rpc(fs,13,ctx?((MOUNT_CONTEXT*)ctx)->node:0,0,0,0,&out,&n);free(out);
    if(NT_SUCCESS(st)&&ctx&&fi)return get_info(fs,ctx,fi);return st;
}
static NTSTATUS set_basic(FSP_FILE_SYSTEM *fs,PVOID ctx,UINT32 attributes,UINT64 creation,UINT64 access,UINT64 write,UINT64 change,FSP_FSCTL_FILE_INFO *fi) {
    if(creation||access||write||change)return STATUS_NOT_SUPPORTED;
    MOUNT_CONTEXT *c=ctx;
    if(attributes==INVALID_FILE_ATTRIBUTES)return get_info(fs,ctx,fi);
    void *out=0;uint32_t n=0;NTSTATUS st=rpc(fs,16,c->node,attributes,0,0,&out,&n);
    if(NT_SUCCESS(st))st=decoded(FALSE,out,n,fi);free(out);return st;
}
static NTSTATUS set_size(FSP_FILE_SYSTEM *fs,PVOID ctx,UINT64 size,BOOLEAN allocation,FSP_FSCTL_FILE_INFO *fi) {
    NTSTATUS st=get_info(fs,ctx,fi);if(!NT_SUCCESS(st))return st;
    if(allocation&&size>=fi->FileSize)return STATUS_SUCCESS;
    return resize_rpc(fs,ctx,size,fi);
}
static NTSTATUS set_delete(FSP_FILE_SYSTEM *fs,PVOID ctx,PWSTR name,BOOLEAN deleting) {
    MOUNT_CONTEXT *c=ctx;if(c->node==1)return STATUS_NOT_SUPPORTED;
    char path[4096];int len=flat(name,path);if(!len)return STATUS_NOT_SUPPORTED;
    void *out=0;uint32_t n=0;NTSTATUS st=rpc(fs,11,c->node,deleting,len,path,&out,&n);free(out);return st;
}
static NTSTATUS rename_file(FSP_FILE_SYSTEM *fs,PVOID ctx,PWSTR name,PWSTR newname,BOOLEAN replace) {
    MOUNT_CONTEXT *c=ctx;if(c->root)return STATUS_NOT_SUPPORTED;
    char from[4096],to[4096],pair[8193];if(!flat(name,from)||!flat(newname,to))return STATUS_NOT_SUPPORTED;
    size_t fromlen=strlen(from),tolen=strlen(to);memcpy(pair,from,fromlen);pair[fromlen]=0;memcpy(pair+fromlen+1,to,tolen);void *out=0;uint32_t n=0;
    NTSTATUS st=rpc(fs,8,c->node,replace,fromlen+1+tolen,pair,&out,&n);free(out);
    if(NT_SUCCESS(st))wcscpy(c->name,newname);return st;
}
typedef struct {WCHAR *name;FSP_FSCTL_FILE_INFO info;} DIRECTORY_ENTRY;
static int ordinal(const void *aa,const void *bb) {
    const DIRECTORY_ENTRY *a=aa,*b=bb;int n=CompareStringOrdinal(a->name,-1,b->name,-1,TRUE);
    return n==CSTR_EQUAL?CompareStringOrdinal(a->name,-1,b->name,-1,FALSE)-CSTR_EQUAL:n-CSTR_EQUAL;
}
static NTSTATUS read_directory(FSP_FILE_SYSTEM *fs,PVOID ctx,PWSTR pattern,PWSTR marker,PVOID buffer,ULONG len,PULONG bytes) {
    MOUNT_CONTEXT *c=ctx;*bytes=0;if(!c->root)return STATUS_NOT_A_DIRECTORY;
    NTSTATUS st=STATUS_SUCCESS;
    if(!FspFileSystemAcquireDirectoryBuffer(&c->directory_buffer,marker==0,&st)) {
        if(NT_SUCCESS(st))FspFileSystemReadDirectoryBuffer(&c->directory_buffer,marker,buffer,len,bytes);return st;
    }
    void *out=0;uint32_t size=0;st=rpc(fs,12,c->node,0,0,0,&out,&size);
    if(!NT_SUCCESS(st)){free(out);FspFileSystemReleaseDirectoryBuffer(&c->directory_buffer);return st;}
    DIRECTORY_ENTRY *entries=0;size_t count=0;uint32_t pos=0;
    while(pos<size) {
        if(size-pos<32){st=STATUS_INTERNAL_ERROR;goto done;}
        uint32_t n;memcpy(&n,(char*)out+pos,4);if(!n||n>size-pos-32){st=STATUS_INTERNAL_ERROR;goto done;}
        DIRECTORY_ENTRY *grown=realloc(entries,(count+1)*sizeof *entries);if(!grown){st=STATUS_INSUFFICIENT_RESOURCES;goto done;}entries=grown;
        int wide=MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,(char*)out+pos+32,n,0,0);if(!wide){st=STATUS_INTERNAL_ERROR;goto done;}
        entries[count].name=calloc(wide+1,sizeof(WCHAR));if(!entries[count].name){st=STATUS_INSUFFICIENT_RESOURCES;goto done;}
        MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,(char*)out+pos+32,n,entries[count].name,wide);
        st=decoded(FALSE,(char*)out+pos+4,28,&entries[count].info);count++;if(!NT_SUCCESS(st))goto done;pos+=32+n;
    }
    qsort(entries,count,sizeof *entries,ordinal);
    for(size_t i=0;i<count;i++) {
        size_t n=wcslen(entries[i].name);FSP_FSCTL_DIR_INFO *di=calloc(1,sizeof *di+n*sizeof(WCHAR));
        if(!di){st=STATUS_INSUFFICIENT_RESOURCES;goto done;}
        di->FileInfo=entries[i].info;di->Size=(UINT16)(sizeof *di+n*sizeof(WCHAR));memcpy(di->FileNameBuf,entries[i].name,n*sizeof(WCHAR));
        BOOL added=FspFileSystemFillDirectoryBuffer(&c->directory_buffer,di,&st);free(di);if(!added)goto done;
    }
done:
    for(size_t i=0;i<count;i++)free(entries[i].name);free(entries);free(out);
    FspFileSystemReleaseDirectoryBuffer(&c->directory_buffer);
    if(NT_SUCCESS(st))FspFileSystemReadDirectoryBuffer(&c->directory_buffer,marker,buffer,len,bytes);return st;
}
static FSP_FILE_SYSTEM_INTERFACE Interface={
    .GetVolumeInfo=volume,.GetSecurityByName=security_name,.Create=create_file,.Open=open_file,.Overwrite=overwrite_file,
    .Cleanup=cleanup,.Close=close_file,.Read=read_file,.Write=write_file,.Flush=flush_file,
    .GetFileInfo=get_info,.SetBasicInfo=set_basic,.SetFileSize=set_size,.SetDelete=set_delete,.Rename=rename_file,.GetSecurity=security_get,.ReadDirectory=read_directory,
};
NTSTATUS vfs_bridge_start(PWSTR mountpoint,VFS_CALLBACK callback,void *context,BOOL case_sensitive,FSP_FILE_SYSTEM **result) {
    BRIDGE *bridge=calloc(1,sizeof *bridge);if(!bridge)return STATUS_INSUFFICIENT_RESOURCES;
    bridge->callback=callback;bridge->context=context;bridge->case_sensitive=case_sensitive;
    HANDLE token=0;DWORD needed=0;PTOKEN_USER user=0;LPWSTR sid=0;
    if(!OpenProcessToken(GetCurrentProcess(),TOKEN_QUERY,&token)) {free(bridge);return STATUS_ACCESS_DENIED;}
    GetTokenInformation(token,TokenUser,0,0,&needed);user=malloc(needed);
    BOOL ok=user&&GetTokenInformation(token,TokenUser,user,needed,&needed)&&ConvertSidToStringSidW(user->User.Sid,&sid);
    WCHAR sddl[1024];if(ok)swprintf(sddl,1024,L"O:%lsG:%lsD:(A;;FA;;;%ls)",sid,sid,sid);
    if(ok)ok=ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl,SDDL_REVISION_1,&bridge->security,&bridge->security_size);
    if(sid)LocalFree(sid);free(user);CloseHandle(token);
    if(!ok) {free(bridge);return STATUS_ACCESS_DENIED;}
    FSP_FSCTL_VOLUME_PARAMS params={0};params.SectorSize=512;params.SectorsPerAllocationUnit=8;params.MaxComponentLength=255;
    params.CaseSensitiveSearch=case_sensitive;params.CasePreservedNames=1;params.UnicodeOnDisk=1;params.PersistentAcls=0;
    params.FileInfoTimeout=0;params.FlushAndPurgeOnCleanup=1;params.UmFileContextIsUserContext2=1;
    wcscpy(params.FileSystemName,L"FactoryVFS");
    FSP_FILE_SYSTEM *fs=0;NTSTATUS status=FspFileSystemCreate(L"WinFsp.Disk",&params,&Interface,&fs);
    if(NT_SUCCESS(status)) {fs->UserContext=bridge;status=FspFileSystemSetMountPoint(fs,mountpoint);}
    if(NT_SUCCESS(status))status=FspFileSystemStartDispatcher(fs,1);
    if(!NT_SUCCESS(status)) {if(fs)FspFileSystemDelete(fs);LocalFree(bridge->security);free(bridge);return status;}
    *result=fs;return STATUS_SUCCESS;
}
void vfs_bridge_stop(FSP_FILE_SYSTEM *fs) {
    BRIDGE *bridge=fs->UserContext;
    FspFileSystemStopDispatcher(fs);FspFileSystemDelete(fs);
    LocalFree(bridge->security);free(bridge);
}
