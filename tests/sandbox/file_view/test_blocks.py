import json
import os
from pathlib import Path
import sys
import time
import tracemalloc
import unittest
from unittest.mock import patch
import pytest
from redpanda.sandbox.versions import native_executable

pytestmark = pytest.mark.process
EXE = native_executable()
if not EXE.is_file():
    pytest.skip("需要已构建的原生 sandbox", allow_module_level=True)

from redpanda.sandbox.file_view import Client,ServiceFailure
from redpanda.sandbox.file_view import publication as pub

CHUNK=65536

class IOCount:
    def __init__(self): self.read_bytes=self.write_bytes=self.max_read=0;self.real=pub.native.open_file
    def open(self,path,**kwargs):
        owner=self
        class Counted:
            def __init__(self,file): self.file=file
            def __getattr__(self,name): return getattr(self.file,name)
            def __enter__(self): return self
            def __exit__(self,*args): self.file.close()
            def read(self,size=-1):
                if size<0: raise AssertionError('unbounded host read')
                owner.max_read=max(owner.max_read,size)
                data=self.file.read(size);owner.read_bytes+=len(data);return data
            def write(self,data): owner.write_bytes+=len(data);return self.file.write(data)
        return Counted(self.real(path,**kwargs))

class Blocks(unittest.TestCase):
    def fixture(self,name,size):
        root=self.root/name;root.mkdir();base=root/'base';base.mkdir()
        # Ordinary bounded files on D:, no sparse flag or huge logical allocation.
        with (base/'large').open('xb') as file:
            for _ in range(size//CHUNK): file.write(b'A'*CHUNK)
        return root,base,root/'store'

    def change(self,client,name,code):
        result=client.run(name,[sys.executable,'-c',code])
        self.assertEqual(result['execution']['exit_code'],0)
        client.request('accept',command_id=name)
        return result['files']['receipt']

    def test_local_write_cost_depends_on_touched_blocks(self):
        metrics=[]
        for label,size in [('32m',32*1024*1024),('256m',256*1024*1024)]:
            root,base,store=self.fixture('blocks_cost_'+label,size)
            with Client(EXE,store,base) as client:
                start=time.monotonic()
                receipt=self.change(client,'c1',f"with open('large','r+b') as f: f.seek({CHUNK-2000});f.write(b'X'*4000)")
                elapsed=(time.monotonic()-start)*1000
                before,after=receipt['changes'][0]['before'],receipt['changes'][0]['after']
                self.assertFalse(before['complete']);self.assertFalse(after['complete'])
                self.assertEqual(set(before['blocks']),{'0','1'});self.assertEqual(set(after['blocks']),{'0','1'})
                host=client.request('info')['host']
                # An unaligned write makes FUSE read the covering pages before
                # the chunk evidence. Windows does not add that read.
                page = os.sysconf('SC_PAGE_SIZE') if os.name != 'nt' else 0
                self.assertLessEqual(host['data_read_bytes'],4*CHUNK+2*page)
                evidence=sum(p.stat().st_size for p in (store/'cas').iterdir())
                self.assertLessEqual(evidence,4*CHUNK)
                with (base/'large').open('r+b') as file:
                    file.seek(CHUNK+5000);file.write(b'H');file.seek(3*1024*1024);file.write(b'U')
                io=IOCount();tracemalloc.start()
                with patch.object(pub.native,'open_file',io.open):
                    self.assertTrue(pub.publish(client)['finalized'])
                    self.assertTrue(pub.undo(client,'c1','preserve')['finalized'])
                _,peak=tracemalloc.get_traced_memory();tracemalloc.stop()
                self.assertLessEqual(io.max_read,CHUNK);self.assertEqual(io.write_bytes,4*CHUNK)
                self.assertLess(peak,4*1024*1024)
                with (base/'large').open('rb') as file:
                    file.seek(CHUNK-2000);self.assertEqual(file.read(4000),b'A'*4000)
                    file.seek(CHUNK+5000);self.assertEqual(file.read(1),b'H')
                    file.seek(3*1024*1024);self.assertEqual(file.read(1),b'U')
                record={'file_bytes':size,'command_ms':elapsed,'command_host_read_bytes':host['data_read_bytes'],'CAS_bytes':evidence,'publication_undo_host_read_bytes':io.read_bytes,'publication_undo_host_write_bytes':io.write_bytes,'max_host_read_request':io.max_read,'python_peak_bytes':peak}
                (root/'metrics.json').write_text(json.dumps(record,indent=2),encoding='utf-8');metrics.append(record)
        self.assertEqual(metrics[0]['command_host_read_bytes'],metrics[1]['command_host_read_bytes'])
        self.assertEqual(metrics[0]['publication_undo_host_read_bytes'],metrics[1]['publication_undo_host_read_bytes'])

    def test_sparse_command_pairs_fold_and_undo_in_reverse(self):
        _,base,store=self.fixture('blocks_batch',4*CHUNK)
        with Client(EXE,store,base) as client:
            self.change(client,'c1',"with open('large','r+b') as f: f.seek(10);f.write(b'XXX');f.seek(10);f.write(b'YYY')")
            self.change(client,'c2',f"with open('large','r+b') as f: f.seek({2*CHUNK+10});f.write(b'ZZZ')")
            self.assertTrue(pub.publish(client)['finalized'])
            self.assertTrue(pub.undo(client,'c2','preserve')['finalized'])
            with (base/'large').open('rb') as file:
                file.seek(10);self.assertEqual(file.read(3),b'YYY')
                file.seek(2*CHUNK+10);self.assertEqual(file.read(3),b'AAA')
            self.assertTrue(pub.undo(client,'c1','original')['finalized'])
            with (base/'large').open('rb') as file:
                file.seek(10);self.assertEqual(file.read(3),b'AAA')

    def test_shrink_records_removed_tail_and_keeps_user_prefix(self):
        size=16*1024*1024
        _,base,store=self.fixture('blocks_truncate',size)
        with Client(EXE,store,base) as client:
            receipt=self.change(client,'c1',f"with open('large','r+b') as f: f.truncate({size-100})")
            self.assertEqual(set(receipt['changes'][0]['before']['blocks']),{str(size//CHUNK-1)})
            with (base/'large').open('r+b') as file:
                file.seek(size-200);file.write(b'H')
            self.assertTrue(pub.publish(client)['finalized']);self.assertEqual((base/'large').stat().st_size,size-100)
            self.assertTrue(pub.undo(client,'c1','original')['finalized']);self.assertEqual((base/'large').stat().st_size,size)
            with (base/'large').open('rb') as file:
                file.seek(size-200);self.assertEqual(file.read(1),b'H')
                file.seek(size-100);self.assertEqual(file.read(100),b'A'*100)

    def test_delete_restore_streams_without_full_file_memory(self):
        size=8*1024*1024
        _,base,store=self.fixture('blocks_delete',size)
        with Client(EXE,store,base) as client:
            self.change(client,'c1',"from pathlib import Path;Path('large').unlink()")
            io=IOCount();tracemalloc.start()
            with patch.object(pub.native,'open_file',io.open):
                self.assertTrue(pub.publish(client)['finalized']);self.assertFalse((base/'large').exists())
                self.assertTrue(pub.undo(client,'c1','original')['finalized'])
            _,peak=tracemalloc.get_traced_memory();tracemalloc.stop()
            self.assertLess(peak,4*1024*1024);self.assertLessEqual(io.max_read,CHUNK)
            self.assertEqual((base/'large').stat().st_size,size)

    def test_previous_store_version_is_refused(self):
        for version in (1,2):
            _,base,store=self.fixture('blocks_old_'+str(version),CHUNK);store.mkdir()
            pub.atomic(store/'config.json',{'version':version,'base':str(base)})
            with self.assertRaisesRegex(ServiceFailure,'workspace config differs'): Client(EXE,store,base)

    def test_append_and_user_tail_follow_explicit_policy(self):
        for policy in ('original','preserve'):
            size=2*CHUNK
            _,base,store=self.fixture('blocks_append_'+policy,size)
            with Client(EXE,store,base) as client:
                receipt=self.change(client,'c1',"with open('large','ab') as f: f.write(b'X'*4000)")
                self.assertEqual(receipt['changes'][0]['before']['blocks'],{'2':None})
                self.assertTrue(pub.publish(client)['finalized'])
                with (base/'large').open('r+b') as file:
                    file.seek(size+10);file.write(b'H')
                self.assertTrue(pub.undo(client,'c1',policy)['finalized'])
                self.assertEqual((base/'large').stat().st_size,size if policy=='original' else size+4000)
                if policy=='preserve':
                    with (base/'large').open('rb') as file:
                        file.seek(size+10);self.assertEqual(file.read(1),b'H')

    def test_recovery_after_first_block_keeps_other_host_blocks(self):
        _,base,store=self.fixture('blocks_recovery',4*CHUNK)
        with Client(EXE,store,base) as client:
            self.change(client,'c1',f"with open('large','r+b') as f: f.seek({CHUNK-1000});f.write(b'X'*2000)")
            real=pub.native.open_file
            class Interrupted:
                def __init__(self,file): self.file=file
                def __getattr__(self,name): return getattr(self.file,name)
                def __enter__(self): return self
                def __exit__(self,*args): self.file.close()
                def write(self,data):
                    self.file.write(data);self.file.flush();os.fsync(self.file.fileno())
                    raise RuntimeError('interrupted after first block')
            def open_file(path,**kwargs):
                file=real(path,**kwargs)
                return Interrupted(file) if kwargs.get('write') else file
            with patch.object(pub.native,'open_file',open_file):
                with self.assertRaises(RuntimeError): pub.publish(client)
            pending=pub.unfinished(store)
        # A user edit outside the selected blocks must survive reconciliation.
        with (base/'large').open('r+b') as file:
            file.seek(3*CHUNK+10);file.write(b'U')
        with Client(EXE,store,base) as client:
            self.assertTrue(pub.execute(client,pending[0])['finalized'])
            with (base/'large').open('rb') as file:
                file.seek(CHUNK-1000);self.assertEqual(file.read(2000),b'X'*2000)
                file.seek(3*CHUNK+10);self.assertEqual(file.read(1),b'U')

    def test_aliases_share_the_first_preimage_in_one_command(self):
        _,base,store=self.fixture('blocks_alias',2*CHUNK)
        os.link(base/'large',base/'alias')
        with Client(EXE,store,base) as client:
            self.change(client,'c1',"with open('large','r+b') as f: f.seek(10);f.write(b'X')\nwith open('alias','r+b') as f: f.seek(20);f.write(b'Y')")
            self.assertTrue(pub.publish(client)['finalized'])
            self.assertTrue(pub.undo(client,'c1','original')['finalized'])
            self.assertTrue(os.path.samefile(base/'large',base/'alias'))
            with (base/'large').open('rb') as file:
                file.seek(10);self.assertEqual(file.read(11),b'A'*11)
