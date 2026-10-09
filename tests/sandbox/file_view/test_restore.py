import json
import os
from pathlib import Path
import sys
import threading
import time
import unittest
import pytest
from redpanda.sandbox.versions import native_executable

pytestmark = pytest.mark.process
EXE = native_executable()
if not EXE.is_file():
    pytest.skip("需要已构建的原生 sandbox", allow_module_level=True)

from redpanda.sandbox.file_view import Client, ServiceFailure
from redpanda.sandbox.file_view import publication as pub

CHUNK = 65536


class RestoreContracts(unittest.TestCase):
    def fixture(self, name, size=CHUNK):
        root = self.root/name
        root.mkdir()
        base = root/'base'
        base.mkdir()
        with (base/'file').open('wb') as file:
            for offset in range(0, size, CHUNK):
                file.write(b'A'*min(CHUNK, size-offset))
        return base, root/'store'

    def change(self, client, command_id, code):
        result = client.run(command_id, [sys.executable, '-c', code])
        self.assertEqual(result['execution']['exit_code'], 0, result)
        self.assertEqual(result['files']['status'], 'sealed')
        client.request('accept', command_id=command_id)
        return result['files']['receipt']

    def read(self, client, probe, offset=0, length=3):
        result = client.begin(probe)
        with (Path(result['mount'])/'file').open('rb') as file:
            file.seek(offset)
            data = file.read(length)
        client.request('finish')
        client.request('discard', command_id=probe)
        return data

    def test_restore_then_restore_the_restore_keeps_one_forward_history(self):
        base, store = self.fixture('restore_roundtrip')
        with Client(EXE, store, base) as client:
            self.change(client, 'edit', "with open('file','r+b') as f: f.write(b'XXX')")
            old_head = client.request('info')['commit']
            result = client.restore('undo', ['edit'], 'preserve')
            self.assertEqual(result['status'], 'sealed', result)
            self.assertEqual(result['receipt']['operation'], {'kind': 'restore', 'targets': ['edit'], 'policy': 'preserve'})
            self.assertEqual(client.request('info')['commit'], old_head)
            self.assertEqual(self.read(client, 'before_accept'), b'XXX')
            client.request('accept', command_id='undo')
            self.assertEqual(self.read(client, 'after_accept'), b'AAA')
            self.assertEqual(client.restore('redo', ['undo'], 'original')['status'], 'sealed')
            client.request('accept', command_id='redo')
            self.assertEqual(self.read(client, 'after_redo'), b'XXX')
            self.assertEqual(client.request('status', command_id='edit')['status'], 'accepted')
            self.assertTrue(pub.publish(client)['finalized'])
            self.assertEqual((base/'file').read_bytes()[:3], b'XXX')

    def test_discarding_restoration_does_not_move_the_view(self):
        base, store = self.fixture('restore_discard')
        with Client(EXE, store, base) as client:
            self.change(client, 'edit', "with open('file','r+b') as f: f.write(b'XXX')")
            self.assertEqual(client.restore('undo', ['edit'], 'original')['status'], 'sealed')
            client.request('discard', command_id='undo')
            self.assertEqual(self.read(client, 'probe'), b'XXX')
            self.assertEqual(client.restore('undo', ['edit'], 'original')['status'], 'discarded')

    def test_range_is_a_contiguous_ordered_suffix(self):
        base, store = self.fixture('restore_range')
        with Client(EXE, store, base) as client:
            self.change(client, 'first', "with open('file','r+b') as f: f.write(b'XXX')")
            self.change(client, 'second', "with open('file','r+b') as f: f.write(b'YYY')")
            old_head = client.request('info')['commit']
            self.assertEqual(client.restore('invalid', ['first'], 'original')['status'], 'range_changed')
            self.assertFalse((store/'commands/invalid').exists())
            self.assertEqual(client.restore('reverse', ['second', 'first'], 'original')['status'], 'range_changed')
            self.assertEqual(client.restore('duplicate', ['second', 'second'], 'original')['status'], 'invalid_request')
            self.assertEqual(client.restore('empty', [], 'original')['status'], 'invalid_request')
            self.assertEqual(client.request('info')['commit'], old_head)
            self.assertEqual(client.restore('undo', ['first', 'second'], 'original')['status'], 'sealed')
            client.request('accept', command_id='undo')
            self.assertEqual(self.read(client, 'probe'), b'AAA')

    def test_user_values_and_undo_of_restore_use_actual_preimage(self):
        for policy, expected in [('original', b'AAA__U'), ('preserve', b'AHA__U')]:
            with self.subTest(policy=policy):
                base, store = self.fixture('restore_human_'+policy)
                with Client(EXE, store, base) as client:
                    self.change(client, 'edit', "with open('file','r+b') as f: f.write(b'XYZ')")
                    pub.publish(client)
                    with (base/'file').open('r+b') as file:
                        file.write(b'XHZ__U')
                    self.assertEqual(client.restore('undo', ['edit'], policy)['status'], 'sealed')
                    client.request('accept', command_id='undo')
                    self.assertEqual(self.read(client, 'probe', length=6), expected)
                    self.assertTrue(pub.publish(client)['finalized'])
                    self.assertEqual((base/'file').read_bytes()[:6], expected)
                    self.assertEqual(client.restore('redo', ['undo'], 'original')['status'], 'sealed')
                    client.request('accept', command_id='redo')
                    self.assertEqual(self.read(client, 'redo_probe', length=6), b'XHZ__U')

    def test_sealed_restoration_survives_lost_reply_and_owner_restart(self):
        base, store = self.fixture('restore_reopen')
        with Client(EXE, store, base) as client:
            self.change(client, 'edit', "with open('file','r+b') as f: f.write(b'XXX')")
            # Do not consume the restore acknowledgement before terminating the owner.
            client.process.stdin.write(json.dumps({'op': 'restore', 'command_id': 'undo', 'targets': ['edit'], 'policy': 'original'})+'\n')
            client.process.stdin.flush()
            deadline = time.monotonic()+20
            while not (store/'commands/undo/sealed.json').exists():
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.01)
            client.process.kill()
            client.process.wait(timeout=20)
        with Client(EXE, store, base) as client:
            self.assertEqual(client.restore('undo', ['edit'], 'original')['status'], 'sealed')
            self.assertEqual(client.restore('undo', ['edit'], 'preserve')['status'], 'request_mismatch')
            client.request('accept', command_id='undo')
            head = client.request('info')['commit']
            self.assertEqual(client.restore('undo', ['edit'], 'original')['status'], 'accepted')
            client.request('accept', command_id='undo')
            self.assertEqual(client.request('info')['commit'], head)
            self.assertEqual(self.read(client, 'probe'), b'AAA')

    def test_interrupted_restore_stays_unknown_and_is_not_reexecuted(self):
        base, store = self.fixture('restore_interrupted', 16*1024*1024)
        client = Client(EXE, store, base)
        try:
            self.change(client, 'edit', "with open('file','r+b') as f: f.write(b'X'*(16*1024*1024))")
            old_head = client.request('info')['commit']
            errors = []
            def restore():
                try:
                    client.restore('undo', ['edit'], 'original')
                except ServiceFailure as error:
                    errors.append(error)
            thread = threading.Thread(target=restore)
            thread.start()
            deadline = time.monotonic()+30
            record = store/'commands/undo/before.json'
            while True:
                if record.exists() and record.stat().st_size > 2:
                    break
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.002)
            client.process.kill()
            client.process.wait(timeout=20)
            thread.join(timeout=20)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(errors), 1)
        finally:
            client.close()
        with Client(EXE, store, base) as client:
            self.assertEqual(client.request('info')['commit'], old_head)
            self.assertEqual(client.restore('undo', ['edit'], 'original')['status'], 'unknown')
            self.assertEqual(client.request('recover', command_id='undo')['status'], 'incomplete_restore')
            self.assertEqual(client.request('accept', command_id='undo')['status'], 'not_sealed')
            self.assertEqual(client.request('info')['commit'], old_head)
            self.assertEqual(client.request('discard', command_id='undo')['status'], 'discarded')
            self.assertEqual(self.read(client, 'probe'), b'XXX')

    def test_corrupt_evidence_is_rejected_before_a_candidate_is_created(self):
        base, store = self.fixture('restore_corrupt')
        client = Client(EXE, store, base)
        try:
            receipt = self.change(client, 'edit', "with open('file','r+b') as f: f.write(b'XXX')")
            head = (store/'HEAD').read_bytes()
            hash_value = next(iter(receipt['changes'][0]['before']['blocks'].values()))
            with (store/'cas'/hash_value).open('r+b') as file:
                file.write(b'!')
            result = client.restore('undo', ['edit'], 'original')
            self.assertEqual(result['status'], 'error')
            self.assertIn('CAS evidence digest differs', result['error'])
            self.assertFalse((store/'commands/undo').exists())
            self.assertEqual((store/'HEAD').read_bytes(), head)
            self.assertEqual(client.request('info')['version'], 3)
        finally:
            client.close()

    def test_native_aliases_share_a_single_restore_result(self):
        base, store = self.fixture('restore_alias')
        os.link(base/'file', base/'alias')
        with Client(EXE, store, base) as client:
            self.change(client, 'edit', "with open('file','r+b') as f: f.write(b'X')\nwith open('alias','r+b') as f: f.seek(10);f.write(b'Y')")
            self.assertEqual(client.restore('undo', ['edit'], 'original')['status'], 'sealed')
            client.request('accept', command_id='undo')
            self.assertEqual(self.read(client, 'probe', length=11), b'A'*11)
            pub.publish(client)
            self.assertTrue(os.path.samefile(base/'file', base/'alias'))
            self.assertEqual((base/'alias').read_bytes()[:11], b'A'*11)

    def test_replaced_object_conflicts_even_when_bytes_match(self):
        base, store = self.fixture('restore_identity')
        with Client(EXE, store, base) as client:
            self.change(client, 'edit', "with open('file','r+b') as f: f.write(b'XXX')")
            pub.publish(client)
            with (base/'replacement').open('wb') as file:
                file.write(b'XXX'+b'A'*(CHUNK-3))
            os.replace(base/'replacement', base/'file')
            result = client.restore('undo', ['edit'], 'original')
            self.assertEqual((result['status'], result['reason']), ('conflict', 'identity_changed'))
            self.assertFalse((store/'commands/undo').exists())

    def test_preserve_keeps_a_human_value_in_the_removed_tail(self):
        base, store = self.fixture('restore_human_tail')
        with Client(EXE, store, base) as client:
            self.change(client, 'edit', "with open('file','ab') as f: f.write(b'X'*4000)")
            pub.publish(client)
            with (base/'file').open('r+b') as file:
                file.seek(CHUNK+10)
                file.write(b'H')
            result = client.restore('undo', ['edit'], 'preserve')
            self.assertEqual(result['status'], 'sealed')
            self.assertEqual(result['receipt']['changes'], [])
            client.request('accept', command_id='undo')
            self.assertEqual(self.read(client, 'probe', CHUNK+10, 1), b'H')
            self.assertTrue(pub.publish(client)['finalized'])
            self.assertEqual((base/'file').stat().st_size, CHUNK+4000)

    def test_completed_restore_can_be_sealed_without_reexecuting_writes(self):
        base, store = self.fixture('restore_completed')
        with Client(EXE, store, base) as client:
            self.change(client, 'edit', "with open('file','r+b') as f: f.write(b'XXX')")
            result = client.restore('undo', ['edit'], 'original')
            self.assertEqual(result['status'], 'sealed')
            before = (store/'commands/undo/before.json').read_bytes()
        # Remove just the final envelope to model interruption before its publication.
        (store/'commands/undo/sealed.json').unlink()
        with Client(EXE, store, base) as client:
            self.assertEqual(client.request('status', command_id='undo')['status'], 'unknown')
            self.assertEqual(client.request('recover', command_id='undo')['status'], 'sealed')
            self.assertEqual((store/'commands/undo/before.json').read_bytes(), before)
            client.request('accept', command_id='undo')
            self.assertEqual(self.read(client, 'probe'), b'AAA')

    def test_restore_cost_tracks_changed_blocks_not_file_size(self):
        metrics = []
        for megabytes in (8, 64):
            base, store = self.fixture('restore_cost_'+str(megabytes), megabytes*1024*1024)
            with Client(EXE, store, base) as client:
                self.change(client, 'edit', f"with open('file','r+b') as f: f.seek({CHUNK-1000});f.write(b'X'*2000)")
                before = client.request('info')['host']['data_read_bytes']
                result = client.restore('undo', ['edit'], 'original')
                self.assertEqual(result['status'], 'sealed')
                receipt = result['receipt']['changes'][0]
                self.assertEqual(set(receipt['before']['blocks']), {'0', '1'})
                self.assertEqual(set(receipt['after']['blocks']), {'0', '1'})
                after = client.request('info')['host']['data_read_bytes']
                metrics.append({'file_bytes': megabytes*1024*1024, 'restore_host_read_bytes': after-before, 'before_blocks': len(receipt['before']['blocks']), 'after_blocks': len(receipt['after']['blocks']), 'CAS_bytes': sum(p.stat().st_size for p in (store/'cas').iterdir())})
        self.assertEqual(metrics[0]['restore_host_read_bytes'], metrics[1]['restore_host_read_bytes'])
        (self.root/'restore-cost.json').write_text(json.dumps(metrics, indent=2), encoding='utf-8')

    def test_append_and_truncate_restore_lengths_without_touching_prefix(self):
        for action in ('append', 'truncate'):
            base, store = self.fixture('restore_length_'+action, 2*CHUNK)
            with Client(EXE, store, base) as client:
                code = "with open('file','ab') as f: f.write(b'X'*4000)" if action=='append' else f"with open('file','r+b') as f: f.truncate({2*CHUNK-100})"
                self.change(client, 'edit', code)
                self.assertEqual(client.restore('undo', ['edit'], 'original')['status'], 'sealed')
                client.request('accept', command_id='undo')
                self.assertEqual(self.read(client, 'probe', 2*CHUNK-100, 100), b'A'*100)
                self.assertTrue(pub.publish(client)['finalized'])
                self.assertEqual((base/'file').stat().st_size, 2*CHUNK)

    def test_created_file_can_be_restored_without_publishing(self):
        base, store = self.fixture('restore_structure')
        with Client(EXE, store, base) as client:
            self.change(client, 'edit', "from pathlib import Path;Path('new').write_bytes(b'new')")
            result = client.restore('undo', ['edit'], 'original')
            self.assertEqual(result['status'], 'sealed')
            client.request('accept', command_id='undo')
            result = client.begin('probe')
            self.assertFalse((Path(result['mount'])/'new').exists())
            client.request('finish');client.request('discard',command_id='probe')
