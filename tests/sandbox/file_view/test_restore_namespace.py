"""Structural restoration contracts on actual mounts, run serially."""
import json
import os
from pathlib import Path
import sys
import threading
import time
import unittest
from . import test_restore as support
import pytest
from redpanda.sandbox.versions import native_executable

pytestmark = pytest.mark.process
EXE = native_executable()
if not EXE.is_file():
    pytest.skip("需要已构建的原生 sandbox", allow_module_level=True)

from redpanda.sandbox.file_view import Client, ServiceFailure
from redpanda.sandbox.file_view import publication as pub


class NamespaceRestoration(unittest.TestCase):
    fixture = support.RestoreContracts.fixture
    change = support.RestoreContracts.change

    def restore_accept(self, client, operation, targets, policy='original'):
        result = client.restore(operation, targets, policy)
        self.assertEqual(result['status'], 'sealed', result)
        self.assertEqual(client.request('accept', command_id=operation)['status'], 'accepted')
        return result['receipt']

    def view(self, client, probe):
        begin = client.begin(probe)
        root = Path(begin['mount'])
        files = {str(p.relative_to(root)).replace('\\', '/'): p.read_bytes()
                 for p in root.rglob('*') if p.is_file()}
        dirs = {str(p.relative_to(root)).replace('\\', '/')
                for p in root.rglob('*') if p.is_dir()}
        client.request('finish')
        client.request('discard', command_id=probe)
        return files, dirs

    def publish(self, client):
        result = pub.publish(client)
        self.assertTrue(result.get('finalized'), result)

    def test_create_delete_roundtrip_before_and_after_publication(self):
        for action in ('create', 'delete'):
            for published in (False, True):
                with self.subTest(action=action, published=published):
                    base, store = self.fixture(f'namespace_{action}_{published}', 100)
                    with Client(support.EXE, store, base) as client:
                        code = "from pathlib import Path;Path('new').write_bytes(b'new')" if action == 'create' else "from pathlib import Path;Path('file').unlink()"
                        self.change(client, 'edit', code)
                        if published: self.publish(client)
                        self.restore_accept(client, 'undo', ['edit'])
                        self.assertEqual(self.view(client, 'probe')[0], {'file': b'A'*100})
                        self.publish(client)
                        self.restore_accept(client, 'redo', ['undo'])
                        expected = {'file': b'A'*100, 'new': b'new'} if action == 'create' else {}
                        self.assertEqual(self.view(client, 'probe_redo')[0], expected)
                        self.publish(client)
                        self.assertEqual({p.name: p.read_bytes() for p in base.iterdir()}, expected)

    def test_rename_and_edit_keep_unowned_user_bytes(self):
        for policy, expected in (('original', b'AAA__U'), ('preserve', b'AHA__U')):
            with self.subTest(policy=policy):
                base, store = self.fixture('namespace_rename_'+policy, 100)
                with Client(support.EXE, store, base) as client:
                    self.change(client, 'edit', "from pathlib import Path;Path('file').rename('renamed')\nwith open('renamed','r+b') as f:f.write(b'XYZ')")
                    self.publish(client)
                    with (base/'renamed').open('r+b') as file: file.write(b'XHZ__U')
                    self.restore_accept(client, 'undo', ['edit'], policy)
                    files, _ = self.view(client, 'probe')
                    self.assertEqual(set(files), {'file'})
                    self.assertEqual(files['file'][:6], expected)
                    self.publish(client)
                    self.restore_accept(client, 'redo', ['undo'])
                    files, _ = self.view(client, 'probe_redo')
                    self.assertEqual(set(files), {'renamed'})
                    self.assertEqual(files['renamed'][:6], b'XHZ__U')
                    self.publish(client)

    def test_rename_replacement_restores_both_objects(self):
        base, store = self.fixture('namespace_replacement', 100)
        (base/'target').write_bytes(b'TARGET')
        with Client(support.EXE, store, base) as client:
            self.change(client, 'edit', "import os;os.replace('file','target')")
            self.restore_accept(client, 'undo', ['edit'])
            self.assertEqual(self.view(client, 'probe')[0], {'file': b'A'*100, 'target': b'TARGET'})
            self.publish(client)
            self.restore_accept(client, 'redo', ['undo'])
            self.assertEqual(self.view(client, 'probe_redo')[0], {'target': b'A'*100})
            self.publish(client)

    def test_nested_created_tree_and_empty_removed_directory_roundtrip(self):
        for action in ('create', 'remove'):
            with self.subTest(action=action):
                base, store = self.fixture('namespace_directory_'+action, 100)
                if action == 'remove': (base/'empty').mkdir()
                with Client(support.EXE, store, base) as client:
                    code = "from pathlib import Path;Path('tree/child').mkdir(parents=True);Path('tree/child/new').write_bytes(b'new')" if action == 'create' else "from pathlib import Path;Path('empty').rmdir()"
                    self.change(client, 'edit', code)
                    self.publish(client)
                    self.restore_accept(client, 'undo', ['edit'])
                    files, dirs = self.view(client, 'probe')
                    self.assertEqual(files, {'file': b'A'*100})
                    self.assertEqual(dirs, set() if action == 'create' else {'empty'})
                    self.publish(client)
                    self.restore_accept(client, 'redo', ['undo'])
                    files, dirs = self.view(client, 'probe_redo')
                    self.assertEqual(dirs, {'tree', 'tree/child'} if action == 'create' else set())
                    self.assertEqual(files, {'file': b'A'*100, 'tree/child/new': b'new'} if action == 'create' else {'file': b'A'*100})
                    self.publish(client)

    def test_user_child_protects_directory_without_protecting_owned_child(self):
        for policy in ('original', 'preserve'):
            with self.subTest(policy=policy):
                base, store = self.fixture('namespace_user_child_'+policy, 100)
                with Client(support.EXE, store, base) as client:
                    self.change(client, 'edit', "from pathlib import Path;Path('tree/child').mkdir(parents=True);Path('tree/child/owned').write_bytes(b'owned')")
                    self.publish(client)
                    (base/'tree/child/user').write_bytes(b'H')
                    result = client.restore('undo', ['edit'], policy)
                    if policy == 'original':
                        self.assertEqual((result['status'], result['reason']), ('conflict', 'directory_contains_user_children'))
                        self.assertFalse((store/'commands/undo').exists())
                    else:
                        self.assertEqual(result['status'], 'sealed', result)
                        client.request('accept', command_id='undo')
                        files, dirs = self.view(client, 'probe')
                        self.assertEqual(files, {'file': b'A'*100, 'tree/child/user': b'H'})
                        self.assertEqual(dirs, {'tree', 'tree/child'})
                        self.publish(client)
                        self.assertEqual((base/'tree/child/user').read_bytes(), b'H')

    def test_user_modified_created_file_has_two_explicit_policies(self):
        for policy in ('original', 'preserve'):
            with self.subTest(policy=policy):
                base, store = self.fixture('namespace_created_human_'+policy, 100)
                with Client(support.EXE, store, base) as client:
                    self.change(client, 'edit', "from pathlib import Path;Path('new').write_bytes(b'X')")
                    self.publish(client)
                    with (base/'new').open('r+b') as file: file.write(b'H')
                    self.restore_accept(client, 'undo', ['edit'], policy)
                    files, _ = self.view(client, 'probe')
                    self.assertEqual(files, {'file': b'A'*100, 'new': b'H'} if policy == 'preserve' else {'file': b'A'*100})
                    self.publish(client)

    def test_user_occupied_old_rename_path_is_kept_or_conflicts(self):
        for policy in ('original', 'preserve'):
            with self.subTest(policy=policy):
                base, store = self.fixture('namespace_occupied_'+policy, 100)
                with Client(support.EXE, store, base) as client:
                    self.change(client, 'edit', "from pathlib import Path;Path('file').rename('renamed')")
                    self.publish(client)
                    (base/'file').write_bytes(b'H')
                    result = client.restore('undo', ['edit'], policy)
                    if policy == 'original':
                        self.assertEqual((result['status'], result['reason']), ('conflict', 'structure_changed'))
                        self.assertFalse((store/'commands/undo').exists())
                    else:
                        self.assertEqual(result['status'], 'sealed', result)
                        client.request('accept', command_id='undo')
                        self.assertEqual(self.view(client, 'probe')[0], {'file': b'H', 'renamed': b'A'*100})

    def test_published_identity_mapping_never_adopts_user_replacement(self):
        base, store = self.fixture('namespace_native_replacement', 100)
        with Client(support.EXE, store, base) as client:
            self.change(client, 'edit', "from pathlib import Path;Path('file').rename('renamed')")
            self.publish(client)
            (base/'replacement').write_bytes(b'A'*100)
            os.replace(base/'replacement', base/'renamed')
            for policy in ('original', 'preserve'):
                result = client.restore('undo_'+policy, ['edit'], policy)
                self.assertEqual((result['status'], result['reason']), ('conflict', 'identity_changed'))
                self.assertFalse((store/'commands'/('undo_'+policy)).exists())

    def test_contiguous_create_edit_rename_delete_range_across_rebases(self):
        base, store = self.fixture('namespace_range', 100)
        with Client(support.EXE, store, base) as client:
            actions = [('create', "from pathlib import Path;Path('new').write_bytes(b'A'*100)"),
                       ('edit', "with open('new','r+b') as f:f.write(b'X')"),
                       ('rename', "from pathlib import Path;Path('new').rename('renamed')"),
                       ('delete', "from pathlib import Path;Path('renamed').unlink()")]
            for operation, code in actions:
                self.change(client, operation, code)
                self.publish(client)
            self.restore_accept(client, 'undo', [operation for operation, _ in actions])
            self.assertEqual(self.view(client, 'probe')[0], {'file': b'A'*100})
            self.publish(client)

    def test_case_only_rename_is_rejected_before_mutation(self):
        if os.name != 'nt':
            self.skipTest('大小写不敏感的名称拒绝只属于 Windows')
        base, store = self.fixture('namespace_case', 100)
        with Client(support.EXE, store, base) as client:
            result = client.run('edit', [sys.executable, '-c', "from pathlib import Path;Path('file').rename('FILE')"])
            self.assertNotEqual(result['execution']['exit_code'], 0, result)
            self.assertEqual(result['files']['receipt']['changes'], [])
            client.request('accept', command_id='edit')
            self.assertEqual(set(self.view(client, 'probe')[0]), {'file'})

    def test_same_bytes_replacement_remains_observable(self):
        base, store = self.fixture('namespace_same_bytes', 100)
        with Client(support.EXE, store, base) as client:
            receipt = self.change(client, 'edit', "from pathlib import Path;Path('file').unlink();Path('file').write_bytes(b'A'*100)")
            self.assertEqual(len(receipt['changes']), 1, receipt)
            change = receipt['changes'][0]
            self.assertNotEqual(change['before']['identity'], change['after']['identity'])
            self.restore_accept(client, 'undo', ['edit'])
            self.assertEqual(self.view(client, 'probe')[0], {'file': b'A'*100})
            self.publish(client)

    def test_rename_cycle_conflicts_before_candidate_creation(self):
        base, store = self.fixture('namespace_cycle', 100)
        (base/'other').write_bytes(b'OTHER')
        with Client(support.EXE, store, base) as client:
            self.change(client, 'edit', "from pathlib import Path;Path('file').rename('temp');Path('other').rename('file');Path('temp').rename('other')")
            result = client.restore('undo', ['edit'], 'original')
            self.assertEqual((result['status'], result['reason']), ('conflict', 'unsupported_rename_cycle'))
            self.assertFalse((store/'commands/undo').exists())
            self.assertEqual(self.view(client, 'probe')[0], {'other': b'A'*100, 'file': b'OTHER'})

    def test_hard_link_namespace_change_is_rejected_before_mutation(self):
        for action in ('unlink', 'rename', 'replace'):
            with self.subTest(action=action):
                base, store = self.fixture('namespace_link_'+action, 100)
                os.link(base/'file', base/'alias')
                (base/'other').write_bytes(b'OTHER')
                with Client(support.EXE, store, base) as client:
                    code = "from pathlib import Path;Path('alias').unlink()" if action == 'unlink' else "from pathlib import Path;Path('alias').rename('renamed')" if action == 'rename' else "import os;os.replace('other','alias')"
                    if os.name == 'nt' and action == 'unlink':
                        # WinFsp commits delete during Cleanup, which has no error
                        # return. The mount owner must expose the failure at finish.
                        with self.assertRaisesRegex(ServiceFailure, 'hard-link namespace changes'):
                            client.run('edit', [sys.executable, '-c', code])
                    else:
                        result = client.run('edit', [sys.executable, '-c', code])
                        self.assertNotEqual(result['execution']['exit_code'], 0, result)
                        self.assertEqual(result['files']['receipt']['changes'], [])
                        client.request('accept', command_id='edit')
                        self.assertEqual(self.view(client, 'probe')[0], {'file': b'A'*100, 'alias': b'A'*100, 'other': b'OTHER'})
                if os.name == 'nt' and action == 'unlink':
                    with Client(support.EXE, store, base) as client:
                        self.assertEqual(client.request('status', command_id='edit')['status'], 'unknown')
                        client.request('discard', command_id='edit')
                        self.assertEqual(self.view(client, 'probe')[0], {'file': b'A'*100, 'alias': b'A'*100, 'other': b'OTHER'})

    def test_interrupted_deleted_file_restoration_cannot_be_accepted(self):
        base, store = self.fixture('namespace_interrupted', 16*1024*1024)
        client = Client(support.EXE, store, base)
        try:
            self.change(client, 'edit', "from pathlib import Path;Path('file').unlink()")
            head = client.request('info')['commit']
            errors = []
            def restore():
                try: client.restore('undo', ['edit'], 'original')
                except ServiceFailure as error: errors.append(error)
            thread = threading.Thread(target=restore)
            thread.start()
            deadline = time.monotonic()+30
            record = store/'commands/undo/before.json'
            while not record.exists() or record.stat().st_size <= 2:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.002)
            client.process.kill();client.process.wait(timeout=20)
            thread.join(timeout=20)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertFalse((store/'commands/undo/restore-complete.json').exists())
        finally:
            client.close()
        with Client(support.EXE, store, base) as client:
            self.assertEqual(client.request('info')['commit'], head)
            self.assertEqual(client.restore('undo', ['edit'], 'original')['status'], 'unknown')
            self.assertEqual(client.request('recover', command_id='undo')['status'], 'incomplete_restore')
            self.assertEqual(client.request('accept', command_id='undo')['status'], 'not_sealed')
            client.request('discard', command_id='undo')
            self.assertEqual(self.view(client, 'probe')[0], {})

    def test_sealed_structural_restore_survives_lost_reply(self):
        base, store = self.fixture('namespace_lost_reply', 100)
        with Client(support.EXE, store, base) as client:
            self.change(client, 'edit', "from pathlib import Path;Path('file').unlink()")
            client.process.stdin.write(json.dumps({'op': 'restore', 'command_id': 'undo', 'targets': ['edit'], 'policy': 'original'})+'\n')
            client.process.stdin.flush()
            deadline = time.monotonic()+20
            while not (store/'commands/undo/sealed.json').exists():
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.01)
            client.process.kill();client.process.wait(timeout=20)
        with Client(support.EXE, store, base) as client:
            self.assertEqual(client.restore('undo', ['edit'], 'original')['status'], 'sealed')
            client.request('accept', command_id='undo')
            head = client.request('info')['commit']
            self.assertEqual(client.restore('undo', ['edit'], 'original')['status'], 'accepted')
            self.assertEqual(client.request('info')['commit'], head)
            self.assertEqual(self.view(client, 'probe')[0], {'file': b'A'*100})
