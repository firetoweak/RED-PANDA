import json
import os
from pathlib import Path
import shutil
import sys
import statistics
import threading
import time
import unittest
from unittest.mock import patch
import pytest
from redpanda.sandbox.versions import native_executable

pytestmark = pytest.mark.process
EXE = native_executable()
if not EXE.is_file():
    pytest.skip("需要已构建的原生 sandbox", allow_module_level=True)

from redpanda.sandbox.file_view import Client, ServiceFailure
from redpanda.sandbox.file_view import publication as pub
from redpanda.sandbox.file_view.native import Running


class Contracts(unittest.TestCase):
    def fixture(self,name):
        root=self.root/name;root.mkdir()
        base=root/'base';base.mkdir()
        (base/'a.txt').write_bytes(b'AAA____AAA')
        return root,base,root/'store'

    def client(self,store,base): return Client(EXE,store,base)

    def change(self,client,name,code):
        result=client.run(name,[sys.executable,'-c',code])
        self.assertEqual(result['execution']['exit_code'],0,result)
        self.assertEqual(result['files']['status'],'sealed',result)
        self.assertEqual(client.request('accept',command_id=name)['status'],'accepted')
        return result

    def test_native_and_file_tools_share_candidate(self):
        _,base,store=self.fixture('native')
        for name in ['delete.txt','rename.txt']: (base/name).write_bytes(name.encode())
        with self.client(store,base) as client:
            begin=client.begin('c1');mount=Path(begin['mount'])
            (mount/'a.txt').write_bytes(b'XYZ____AAA')
            code="from pathlib import Path;assert Path('a.txt').read_bytes()==b'XYZ____AAA';Path('dir').mkdir();Path('dir/new.txt').write_bytes(b'new');Path('delete.txt').unlink();Path('rename.txt').rename('renamed.txt')"
            running=Running([sys.executable,'-c',code],mount,client.command_environment,store/'commands/c1')
            self.assertEqual(running.wait()['exit_code'],0)
            self.assertEqual(client.request('finish')['status'],'sealed')
            self.assertEqual((base/'a.txt').read_bytes(),b'AAA____AAA')
            client.request('accept',command_id='c1')
            self.assertEqual(pub.publish(client)['state'],'complete')
            self.assertEqual((base/'a.txt').read_bytes(),b'XYZ____AAA')
            self.assertFalse((base/'delete.txt').exists())
            self.assertEqual((base/'dir/new.txt').read_bytes(),b'new')
            self.assertEqual((base/'renamed.txt').read_bytes(),b'rename.txt')
            self.assertEqual(pub.undo(client,'c1','original')['state'],'complete')
            self.assertEqual((base/'a.txt').read_bytes(),b'AAA____AAA')
            self.assertTrue((base/'delete.txt').exists());self.assertTrue((base/'rename.txt').exists())
            self.assertFalse((base/'dir').exists())

    def test_preserve_restores_only_matching_command_bytes(self):
        _,base,store=self.fixture('preserve')
        with self.client(store,base) as client:
            self.change(client,'c1',"from pathlib import Path;Path('a.txt').write_bytes(b'XYZ____AAA')")
            self.assertEqual(pub.publish(client)['state'],'complete')
            (base/'a.txt').write_bytes(b'XHZ__U_AAA')
            self.assertEqual(pub.undo(client,'c1','preserve')['state'],'complete')
            self.assertEqual((base/'a.txt').read_bytes(),b'AHA__U_AAA')

    def test_original_overwrites_only_command_changed_bytes(self):
        _,base,store=self.fixture('original')
        with self.client(store,base) as client:
            self.change(client,'c1',"from pathlib import Path;Path('a.txt').write_bytes(b'XYZ____AAA')")
            pub.publish(client);(base/'a.txt').write_bytes(b'HHH__U_AAA')
            pub.undo(client,'c1','original')
            self.assertEqual((base/'a.txt').read_bytes(),b'AAA__U_AAA')

    def test_user_aba_result_allows_restore(self):
        _,base,store=self.fixture('aba')
        with self.client(store,base) as client:
            self.change(client,'c1',"from pathlib import Path;Path('a.txt').write_bytes(b'XYZ____AAA')")
            pub.publish(client)
            (base/'a.txt').write_bytes(b'HHH____AAA');(base/'a.txt').write_bytes(b'XYZ____AAA')
            pub.undo(client,'c1','preserve')
            self.assertEqual((base/'a.txt').read_bytes(),b'AAA____AAA')

    def test_publication_preserves_unrelated_user_bytes(self):
        _,base,store=self.fixture('unrelated')
        with self.client(store,base) as client:
            self.change(client,'c1',"from pathlib import Path;Path('a.txt').write_bytes(b'XYZ____AAA')")
            (base/'a.txt').write_bytes(b'AAA__U_AAA')
            self.assertEqual(pub.publish(client)['state'],'complete')
            self.assertEqual((base/'a.txt').read_bytes(),b'XYZ__U_AAA')

    def test_overlapping_user_bytes_block_publication(self):
        _,base,store=self.fixture('overlap')
        with self.client(store,base) as client:
            self.change(client,'c1',"from pathlib import Path;Path('a.txt').write_bytes(b'XYZ____AAA')")
            (base/'a.txt').write_bytes(b'HHH____AAA')
            self.assertEqual(pub.publish(client)['status'],'conflict')
            self.assertEqual((base/'a.txt').read_bytes(),b'HHH____AAA')

    def test_batch_and_reverse_undo(self):
        _,base,store=self.fixture('batch')
        with self.client(store,base) as client:
            for name,value in [('c1','XXX'),('c2','YYY')]:
                self.change(client,name,f"from pathlib import Path;Path('a.txt').write_bytes(b'{value}____AAA')")
            self.assertEqual(pub.publish(client)['state'],'complete')
            self.assertEqual((base/'a.txt').read_bytes(),b'YYY____AAA')
            self.assertEqual(pub.undo(client,'c1','original')['status'],'later_command_active')
            pub.undo(client,'c2','preserve');self.assertEqual((base/'a.txt').read_bytes(),b'XXX____AAA')
            pub.undo(client,'c1','preserve');self.assertEqual((base/'a.txt').read_bytes(),b'AAA____AAA')

    def test_equal_bytes_replacement_conflicts(self):
        _,base,store=self.fixture('identity')
        with self.client(store,base) as client:
            self.change(client,'c1',"from pathlib import Path;Path('a.txt').write_bytes(b'XYZ____AAA')")
            pub.publish(client)
            (base/'other').write_bytes(b'XYZ____AAA');os.replace(base/'other',base/'a.txt')
            self.assertEqual(pub.undo(client,'c1','original')['status'],'conflict')

    def test_pinned_reuse_and_cold_corruption_refusal(self):
        _,base,store=self.fixture('cache')
        with self.client(store,base) as client:
            self.change(client,'c1',"from pathlib import Path;Path('a.txt').write_bytes(b'XYZ____AAA')")
            h=client.request('info')['digest'];artifact=store/'artifacts'/(h+'.db')
            with self.assertRaises(OSError): artifact.open('r+b')
            if os.name == 'nt':
                with self.assertRaises(OSError): artifact.rename(artifact.with_suffix('.moved'))
            self.assertEqual(client.begin('c2')['parent_hashed_bytes'],0)
            client.request('finish');client.request('discard',command_id='c2')
        with artifact.open('r+b') as file:
            file.seek(100);value=file.read(1);file.seek(100);file.write(bytes([value[0]^1]))
        with self.assertRaises(ServiceFailure): self.client(store,base)

    def test_owner_death_requires_explicit_recovery(self):
        _,base,store=self.fixture('owner_crash')
        client=self.client(store,base);begin=client.begin('c1')
        with (Path(begin['mount'])/'a.txt').open('r+b') as file:
            file.write(b'XYZ');file.flush();os.fsync(file.fileno())
        client.process.kill();client.process.wait(timeout=20);client.close()
        with self.client(store,base) as client:
            self.assertEqual(client.request('status',command_id='c1')['status'],'unknown')
            self.assertEqual(client.request('recover',command_id='c1')['status'],'sealed')
            client.request('accept',command_id='c1');pub.publish(client)
            self.assertEqual((base/'a.txt').read_bytes(),b'XYZ____AAA')

    def test_nonzero_does_not_choose_acceptance(self):
        _,base,store=self.fixture('nonzero')
        with self.client(store,base) as client:
            result=client.run('c1',[sys.executable,'-c',"from pathlib import Path;Path('new').write_bytes(b'partial');raise SystemExit(23)"])
            self.assertEqual(result['execution']['exit_code'],23)
            self.assertEqual(client.request('status',command_id='c1')['status'],'sealed')
            client.request('discard',command_id='c1');self.assertFalse((base/'new').exists())

    def test_job_drains_descendants(self):
        _,base,store=self.fixture('job')
        with self.client(store,base) as client:
            code="import subprocess,sys,time;subprocess.Popen([sys.executable,'-c',\"import time;from pathlib import Path;time.sleep(1);Path('escaped').write_bytes(b'bad')\"]);time.sleep(.1)"
            result=client.run('c1',[sys.executable,'-c',code])
            self.assertEqual(result['execution']['exit_code'],0)
            time.sleep(1.2)
            self.assertFalse(any(c['path']=='/escaped' for c in result['files']['receipt']['changes']))
            client.request('discard',command_id='c1')

    def test_timeout_preserves_partial_evidence(self):
        _,base,store=self.fixture('timeout')
        with self.client(store,base) as client:
            result=client.run('c1',[sys.executable,'-c',"from pathlib import Path;import time;Path('new').write_bytes(b'partial');time.sleep(10)"],timeout=.3)
            self.assertTrue(result['execution']['timed_out'])
            self.assertEqual(result['files']['status'],'sealed')
            client.request('discard',command_id='c1')

    def test_host_partial_completion_can_be_queried_and_recovered(self):
        _,base,store=self.fixture('host_crash')
        with self.client(store,base) as client:
            self.change(client,'c1',"from pathlib import Path;Path('a.txt').write_bytes(b'XYZ____AAA');Path('new').write_bytes(b'new')")
            original=pub.atomic
            def fail(path,value):
                original(path,value)
                if type(value) is dict and value.get('state')=='prepared' and any(s['state']=='done' for s in value.get('steps',[])):
                    raise RuntimeError('simulated lost acknowledgment after one host step')
            with patch.object(pub,'atomic',fail):
                with self.assertRaises(RuntimeError): pub.publish(client)
            pending=pub.unfinished(store);self.assertEqual(len(pending),1)
            self.assertTrue(any(s['state']=='done' for s in pub.load(pending[0])['steps']))
            with self.assertRaises(RuntimeError): client.begin('c2')
        with self.client(store,base) as client:
            self.assertEqual(pub.execute(client,pending[0])['state'],'complete')
            self.assertEqual((base/'a.txt').read_bytes(),b'XYZ____AAA')
            self.assertEqual((base/'new').read_bytes(),b'new')

    def test_corrupt_cas_remains_an_error(self):
        _,base,store=self.fixture('cas')
        with self.client(store,base) as client:
            self.change(client,'c1',"from pathlib import Path;Path('a.txt').write_bytes(b'XYZ____AAA')")
            image=client.request('status',command_id='c1')['receipt']['changes'][0]['after']
            (store/'cas'/next(v for v in image['blocks'].values() if v is not None)).write_bytes(b'corrupt')
            with self.assertRaises(ValueError): pub.publish(client)
            self.assertEqual((base/'a.txt').read_bytes(),b'AAA____AAA')

    def test_chain_growth_and_writeback_rebase(self):
        root,base,store=self.fixture('chain');metrics=[]
        with self.client(store,base) as client:
            for i in range(32):
                begin=client.begin(f'c{i}');self.assertEqual(begin['parent_hashed_bytes'],0)
                (Path(begin['mount'])/'a.txt').write_bytes(f'{i:03d}____AAA'.encode())
                client.request('finish');client.request('accept',command_id=f'c{i}')
                start=time.monotonic();client.request('inspect',path='/a.txt')
                metrics.append({'depth':i+1,'begin_ms':begin['begin_ms'],'inspect_ms':(time.monotonic()-start)*1000})
        start=time.monotonic()
        with self.client(store,base) as client:
            cold={'milliseconds':(time.monotonic()-start)*1000,**client.ready}
            self.assertEqual(client.request('info')['depth'],32)
            self.assertEqual(pub.publish(client)['state'],'complete')
            self.assertEqual(client.request('info')['depth'],0)
            self.assertEqual((base/'a.txt').read_bytes(),b'031____AAA')
            pruned=client.request('prune')
            self.assertEqual(pruned['removed_artifacts'],32)
            self.assertEqual(pub.undo(client,'c31','preserve')['state'],'complete')
            self.assertEqual((base/'a.txt').read_bytes(),b'030____AAA')
        (root/'metrics.json').write_text(json.dumps({'warm':metrics,'cold':cold,'after_writeback_depth':0,'prune':pruned},indent=2),encoding='utf-8')

    def test_service_environment_does_not_replace_command_environment(self):
        _,base,store=self.fixture('environment')
        service=dict(os.environ,VIEW_ORIGIN='service')
        command=dict(os.environ,VIEW_ORIGIN='command')
        with Client(EXE,store,base,service_environment=service,command_environment=command) as client:
            result=client.run('c1',[sys.executable,'-c',"import os;from pathlib import Path;Path('env').write_text(os.environ['VIEW_ORIGIN'])"])
            self.assertEqual(result['execution']['exit_code'],0)
            client.request('accept',command_id='c1');pub.publish(client)
            self.assertEqual((base/'env').read_text(),'command')

    def test_explicit_cancellation_does_not_accept_or_discard(self):
        _,base,store=self.fixture('cancel')
        cancel=threading.Event()
        timer=threading.Timer(.4,cancel.set)
        with self.client(store,base) as client:
            timer.start()
            try:
                result=client.run('c1',[sys.executable,'-c',"import time;from pathlib import Path;Path('new').write_bytes(b'partial');time.sleep(10)"],cancel_event=cancel)
            finally: timer.join()
            self.assertTrue(result['execution']['interrupted'])
            self.assertFalse(result['execution']['timed_out'])
            self.assertEqual(client.request('status',command_id='c1')['status'],'sealed')
            self.assertTrue(pub.load(store/'commands/c1/execution.json')['interrupted'])
            client.request('discard',command_id='c1')

    def test_acceptance_can_be_queried_after_lost_reply(self):
        _,base,store=self.fixture('lost_accept')
        client=self.client(store,base)
        result=client.run('c1',[sys.executable,'-c',"from pathlib import Path;Path('a.txt').write_bytes(b'XYZ____AAA')"])
        self.assertEqual(result['files']['status'],'sealed')
        client.process.stdin.write(json.dumps({'op':'accept','command_id':'c1'})+'\n');client.process.stdin.flush()
        deadline=time.monotonic()+10
        while not (store/'HEAD').exists():
            self.assertIsNone(client.process.poll())
            if time.monotonic()>deadline: self.fail('accept did not commit')
            time.sleep(.002)
        client.process.kill();client.process.wait(timeout=20);client.close()
        with self.client(store,base) as client:
            self.assertEqual(client.request('status',command_id='c1')['status'],'accepted')
            self.assertEqual(client.request('accept',command_id='c1')['status'],'accepted')
            pub.publish(client)
            self.assertEqual((base/'a.txt').read_bytes(),b'XYZ____AAA')

    def test_invalid_control_input_is_rejected_without_losing_view(self):
        _,base,store=self.fixture('invalid')
        with self.client(store,base) as client:
            self.assertEqual(client.request('info',unexpected=True)['status'],'invalid_request')
            self.assertEqual(client.begin('c1')['status'],'active')
            self.assertEqual(client.begin('c2')['status'],'busy')
            client.request('finish');client.request('discard',command_id='c1')

    def test_user_modified_created_file_is_preserved(self):
        _,base,store=self.fixture('new_user')
        with self.client(store,base) as client:
            self.change(client,'c1',"from pathlib import Path;Path('new').write_bytes(b'X')")
            pub.publish(client);(base/'new').write_bytes(b'H')
            self.assertEqual(pub.undo(client,'c1','preserve')['state'],'complete')
            self.assertEqual((base/'new').read_bytes(),b'H')

    def test_original_can_remove_command_created_user_modified_file(self):
        _,base,store=self.fixture('new_original')
        with self.client(store,base) as client:
            self.change(client,'c1',"from pathlib import Path;Path('new').write_bytes(b'X')")
            pub.publish(client);(base/'new').write_bytes(b'H')
            self.assertEqual(pub.undo(client,'c1','original')['state'],'complete')
            self.assertFalse((base/'new').exists())

    def test_hard_link_publication_and_undo_preserve_alias(self):
        _,base,store=self.fixture('hardlink')
        os.link(base/'a.txt',base/'alias.txt')
        with self.client(store,base) as client:
            self.change(client,'c1',"from pathlib import Path;Path('alias.txt').write_bytes(b'XYZ____AAA')")
            pub.publish(client)
            self.assertEqual((base/'a.txt').read_bytes(),b'XYZ____AAA')
            self.assertTrue(os.path.samefile(base/'a.txt',base/'alias.txt'))
            pub.undo(client,'c1','preserve')
            self.assertEqual((base/'a.txt').read_bytes(),b'AAA____AAA')
            self.assertTrue(os.path.samefile(base/'a.txt',base/'alias.txt'))

    def test_partial_existing_file_write_can_be_reconciled(self):
        _,base,store=self.fixture('partial_file')
        with self.client(store,base) as client:
            self.change(client,'c1',"from pathlib import Path;Path('a.txt').write_bytes(b'XYZ____AAA')")
            real=pub.native.open_file
            class BrokenWrite:
                def __init__(self,file): self.file=file
                def __getattr__(self,name): return getattr(self.file,name)
                def __enter__(self): return self
                def __exit__(self,*args): self.file.close()
                def write(self,data):
                    self.file.write(data[:2]);self.file.flush();os.fsync(self.file.fileno())
                    raise RuntimeError('simulated process loss mid-write')
            def open_file(path,**kwargs):
                file=real(path,**kwargs)
                return BrokenWrite(file) if kwargs.get('write') else file
            with patch.object(pub.native,'open_file',open_file):
                with self.assertRaises(RuntimeError): pub.publish(client)
            pending=pub.unfinished(store);self.assertEqual(len(pending),1)
            self.assertEqual((base/'a.txt').read_bytes(),b'XYA____AAA')
        with self.client(store,base) as client:
            self.assertEqual(pub.execute(client,pending[0])['state'],'complete')
            self.assertEqual((base/'a.txt').read_bytes(),b'XYZ____AAA')

    def test_reconciliation_does_not_overwrite_new_user_value(self):
        _,base,store=self.fixture('recovery_user')
        with self.client(store,base) as client:
            self.change(client,'c1',"from pathlib import Path;Path('a.txt').write_bytes(b'XYZ____AAA');Path('new').write_bytes(b'new')")
            original=pub.atomic
            def fail(path,value):
                original(path,value)
                if type(value) is dict and any(s['state']=='done' for s in value.get('steps',[])): raise RuntimeError('lost caller')
            with patch.object(pub,'atomic',fail):
                with self.assertRaises(RuntimeError): pub.publish(client)
            pending=pub.unfinished(store)
            (base/'a.txt').write_bytes(b'HHH____AAA')
            self.assertEqual(pub.execute(client,pending[0])['status'],'conflict')
            self.assertEqual((base/'a.txt').read_bytes(),b'HHH____AAA')

    def test_parent_size_affects_cold_validation_but_not_warm_candidate(self):
        results=[]
        for label,size in [('small',524288),('large',8388608)]:
            root,base,store=self.fixture('cost_'+label)
            warm=[]
            with self.client(store,base) as client:
                self.change(client,'seed',f"import random;from pathlib import Path;Path('payload').write_bytes(random.Random(7).randbytes({size}))")
                baseline_host=client.request('info')['host']
                for i in range(5):
                    begin=client.begin(f'empty{i}');self.assertEqual(begin['parent_hashed_bytes'],0)
                    warm.append(begin['begin_ms']);client.request('finish');client.request('discard',command_id=f'empty{i}')
                host=client.request('info')['host']
                self.assertEqual(host['directory_enumerations'],baseline_host['directory_enumerations'])
                self.assertEqual(host['data_read_bytes'],baseline_host['data_read_bytes'])
            cold=[]
            for _ in range(5):
                start=time.monotonic()
                with self.client(store,base) as client:
                    cold.append((time.monotonic()-start)*1000)
                    hashed=client.ready['cold_hashed_bytes']
            data={'payload_bytes':size,'warm_ms':warm,'cold_ms':cold,'warm_median_ms':statistics.median(warm),'cold_median_ms':statistics.median(cold),'cold_hashed_bytes':hashed,'host_baseline':baseline_host,'host_after_empty_candidates':host}
            (root/'metrics.json').write_text(json.dumps(data,indent=2),encoding='utf-8');results.append(data)
        self.assertGreater(results[1]['cold_hashed_bytes'],results[0]['cold_hashed_bytes'])

    def test_persistent_states_are_not_downgraded_to_normal_results(self):
        _,base,store=self.fixture('corrupt_state')
        with self.client(store,base) as client:
            pub.atomic(store/'host-index.json',{'version':3,'active':[123],'bindings':{}})
            with self.assertRaisesRegex(ValueError,'invalid command id'): pub.read_index(store)
            pub.atomic(store/'host-index.json',{'version':3,'active':[],'bindings':{}})
            folder=store/'host';folder.mkdir()
            path=folder/'broken.json'
            pub.atomic(path,{'version':3,'kind':'publish','commands':['c1'],'commit':'00000000-0000-0000-0000-000000000000','steps':[],'state':'invented','finalized':False})
            with self.assertRaisesRegex(ValueError,'invalid transaction'): client.begin('c1')
            self.assertFalse((store/'commands/c1').exists())
        # A malformed HEAD commit must fail startup, not become an empty view.
        path=store/'commits/00000000-0000-0000-0000-000000000000.json'
        pub.atomic(path,{'previous':None,'digest':None,'command_id':None,'kind':'invented'})
        (store/'HEAD').write_text(path.stem,encoding='utf-8')
        with self.assertRaisesRegex(ServiceFailure,'invalid commit kind'): self.client(store,base)

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell 工作流")
    def test_real_powershell_python_git_workflow(self):
        _,base,store=self.fixture('workflow')
        powershell=shutil.which('pwsh') or shutil.which('powershell');git=shutil.which('git')
        self.assertIsNotNone(powershell);self.assertIsNotNone(git)
        with self.client(store,base) as client:
            result=client.run('ps',[powershell,'-NoProfile','-NonInteractive','-Command',"Set-Content -LiteralPath hello.py -Value 'print(42)' -Encoding utf8"])
            self.assertEqual(result['execution']['exit_code'],0)
            client.request('accept',command_id='ps')
            result=client.run('compile',[sys.executable,'-m','compileall','-q','.'])
            self.assertEqual(result['execution']['exit_code'],0)
            client.request('accept',command_id='compile')
            result=client.run('git',[git,'-c','core.hideDotFiles=false','init','-q'])
            self.assertEqual(result['execution']['exit_code'],0,(store/'commands/git/stderr.bin').read_bytes())
            client.request('accept',command_id='git')
            self.assertEqual(pub.publish(client)['state'],'complete')
            self.assertTrue((base/'.git/HEAD').exists())
            self.assertTrue(list((base/'__pycache__').glob('*.pyc')))
