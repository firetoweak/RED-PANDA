from __future__ import annotations
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
from .native import Running

class ServiceFailure(RuntimeError):
    pass

class Client:
    """One owner and one synchronous control stream; native tools run in callers."""
    def __init__(self, executable, store, base, *, command_environment=None, service_environment=None):
        self.store, self.base = Path(store).resolve(), Path(base).resolve()
        if os.name == 'nt' and not str(self.store).startswith('\\\\?\\'):
            self.store = Path('\\\\?\\' + str(self.store))
        self.store.mkdir(parents=True,exist_ok=True)
        self.command_environment = dict(os.environ if command_environment is None else command_environment)
        self.log = (self.store/'service.log').open('ab')
        self.process = subprocess.Popen([str(executable),str(self.store),str(self.base)],cwd=self.store,
            env=dict(os.environ if service_environment is None else service_environment),stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=self.log,text=True,encoding='utf-8',creationflags=0x08000000)
        self.replies = queue.Queue()
        self.lock = threading.Lock()
        def read():
            for line in self.process.stdout: self.replies.put(line)
            self.replies.put(None)
        self.reader = threading.Thread(target=read,daemon=True)
        self.reader.start()
        try:
            self.ready = self._reply()
            if self.ready.get('status')!='ready' or self.ready.get('version')!=3:
                raise ServiceFailure('unsupported handshake')
        except BaseException:
            if self.process.poll() is None:
                self.process.terminate()
                self.process.wait(timeout=20)
            self.process.stdin.close()
            self.process.stdout.close()
            self.reader.join(timeout=10)
            self.log.close()
            raise

    def _reply(self):
        line = self.replies.get(timeout=60)
        if line is None:
            self.process.wait(timeout=10)
            raise ServiceFailure(f'service exit {self.process.returncode}: '+(self.store/'service.log').read_text(encoding='utf-8',errors='replace'))
        value = json.loads(line)
        if type(value) is not dict: raise ServiceFailure('response must be an object')
        return value

    def request(self, op, **fields):
        with self.lock:
            self.process.stdin.write(json.dumps({'op':op,**fields},ensure_ascii=False)+'\n')
            self.process.stdin.flush()
            return self._reply()

    def begin(self, command_id):
        from .publication import unfinished
        if unfinished(self.store): raise RuntimeError('unfinished host transaction requires reconciliation')
        return self.request('begin',command_id=command_id)

    def run(self, command_id, argv, *, timeout=60, environment=None, cancel_event=None):
        begin = self.begin(command_id)
        if begin['status']!='active': return begin
        running = Running(argv,begin['mount'],self.command_environment if environment is None else environment,self.store/'commands'/command_id)
        result = running.wait(timeout,cancel_event)
        from .publication import atomic
        atomic(self.store/'commands'/command_id/'execution.json',result)
        # A nonzero exit remains evidence; accepting partial effects is an explicit choice.
        return {'execution':result,'files':self.request('finish')}

    def restore(self, command_id, targets, policy):
        from .publication import unfinished
        if unfinished(self.store): raise RuntimeError('unfinished host transaction requires reconciliation')
        return self.request('restore',command_id=command_id,targets=targets,policy=policy)

    def close(self):
        if self.process.poll() is None:
            self.request('shutdown')
            self.process.wait(timeout=20)
        self.process.stdin.close()
        self.process.stdout.close()
        self.reader.join(timeout=10)
        self.log.close()

    def __enter__(self): return self
    def __exit__(self,*_): self.close()
