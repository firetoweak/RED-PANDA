"""Linux permission metadata uses the same publication and restoration contract."""
import os
from pathlib import Path
import stat
import sys

import pytest
from redpanda.filesystem.file_view import native, publication as pub
from redpanda.filesystem.file_view.client import native_executable
from tests.fixtures.file_view import CommandClient

pytestmark = [pytest.mark.process, pytest.mark.skipif(sys.platform != 'linux', reason='Linux permission metadata')]


@pytest.mark.parametrize('name', ['file','directory'])
@pytest.mark.parametrize('mode', [0o700,0])
@pytest.mark.parametrize('route', ['host','view'])
def test_permission_only_changes_restore_without_reading_or_rewriting_content(tmp_path,name,mode,route):
    base=tmp_path/'project';base.mkdir();(base/'file').write_bytes(b'unobserved')
    (base/'directory').mkdir();path=base/name;before=stat.S_IMODE(path.stat().st_mode)
    identity=native.directory_identity(path)
    try:
        with CommandClient(native_executable(),tmp_path/'store',base) as client:
            mount=Path(client.begin('mode')['mount']);os.chmod(mount/name,mode)
            change,=client.request('finish')['receipt']['changes']
            assert change['after']['mode']==mode
            assert change['before']['blocks']==change['after']['blocks']=={}
            client.request('accept',command_id='mode')
            assert pub.publish(client)['state']=='complete'
            assert stat.S_IMODE(path.stat().st_mode)==mode
            if route=='host': assert pub.undo(client,'mode','original')['state']=='complete'
            else:
                result=client.restore('undo',['mode'],'original');assert result['status']=='sealed',result
                client.request('accept',command_id='undo');assert pub.publish(client)['state']=='complete'
            assert stat.S_IMODE(path.stat().st_mode)==before
            assert native.directory_identity(path)==identity
    finally:
        os.chmod(path,before)
    assert (base/'file').read_bytes()==b'unobserved'
