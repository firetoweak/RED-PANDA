#define _WIN32_WINNT 0x0602
#include <windows.h>
#include <stdio.h>
#include <string.h>
#include "sqlite3.h"
static SYNCHRONIZATION_BARRIER barrier;
typedef struct {const char *path;sqlite3 *db;int error;} READER;
static int sql(sqlite3 *db,const char *text) {return sqlite3_exec(db,text,0,0,0);}
static int open_db(const char *path,sqlite3 **db) {
    int rc=sqlite3_open(path,db);if(rc)return rc;
    sqlite3_busy_timeout(*db,5000);
    return sql(*db,"PRAGMA journal_mode=WAL;PRAGMA wal_autocheckpoint=0;PRAGMA synchronous=NORMAL");
}
static DWORD WINAPI read_loop(void *arg) {
    READER *reader=arg;reader->error=open_db(reader->path,&reader->db);
    if(reader->error)return 1;
    for(int i=0;i<10000;i++) {
        EnterSynchronizationBarrier(&barrier,0);
        sqlite3_stmt *stmt=0;
        int rc=sqlite3_prepare_v2(reader->db,"SELECT value FROM example",-1,&stmt,0);
        if(rc!=SQLITE_OK) {reader->error=rc;return 1;}
        rc=sqlite3_step(stmt);
        if(rc!=SQLITE_ROW) {reader->error=rc;sqlite3_finalize(stmt);return 1;}
        sqlite3_finalize(stmt);
    }
    return 0;
}
static int result(void *arg,int count,char **values,char **names) {
    printf("checkpoint=");for(int i=0;i<count;i++)printf("%s%s",i?",":"",values[i]);puts("");
    return 0;
}
int main(int argc,char **argv) {
    char folder[MAX_PATH],file[MAX_PATH],path[MAX_PATH+10];
    GetTempPathA(MAX_PATH,folder);GetTempFileNameA(folder,"wal",0,file);
    int extended=argc>1&&!strcmp(argv[1],"--extended");
    snprintf(path,sizeof path,extended?"\\\\?\\%s":"%s",file);
    sqlite3 *db=0;if(open_db(path,&db)!=SQLITE_OK)return 1;
    if(sql(db,"CREATE TABLE example(value INTEGER);INSERT INTO example VALUES(42)")!=SQLITE_OK)return 2;
    InitializeSynchronizationBarrier(&barrier,2,-1);
    READER readers[2]={{path,0,0},{path,0,0}};
    HANDLE threads[2];for(int i=0;i<2;i++)threads[i]=CreateThread(0,0,read_loop,&readers[i],0,0);
    WaitForMultipleObjects(2,threads,TRUE,INFINITE);
    printf("sqlite=%s extended=%d completed_reads=20000 txn_states=%d,%d,%d errors=%d,%d\n",sqlite3_libversion(),extended,
        sqlite3_txn_state(db,"main"),sqlite3_txn_state(readers[0].db,"main"),sqlite3_txn_state(readers[1].db,"main"),readers[0].error,readers[1].error);
    ULONGLONG started=GetTickCount64();
    int rc=sqlite3_exec(db,"PRAGMA wal_checkpoint(TRUNCATE)",result,0,0);
    printf("rc=%d elapsed_ms=%llu\n",rc,GetTickCount64()-started);
    for(int i=0;i<2;i++){sqlite3_close(readers[i].db);CloseHandle(threads[i]);}
    sqlite3_close(db);DeleteSynchronizationBarrier(&barrier);DeleteFileA(file);
    return rc;
}
