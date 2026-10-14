import json
from pathlib import Path
import shutil

import pytest

from scripts import execution_runtime as runtime


def build(root):
    config=root/"native/sandbox/release.json";config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"version":"0.4.2","repository":"example/project"}))
    directory=root/"redpanda/execution/bin";directory.mkdir(parents=True)
    for name in runtime.REQUIRED:
        path=directory/name;path.parent.mkdir(exist_ok=True);path.write_bytes(name.encode())
    record={"profile":"release","target":"windows-x86_64","files":{name:runtime.delivery.digest(directory/name) for name in runtime.REQUIRED}}
    (directory/"BUILD.json").write_text(json.dumps(record))
    return directory


def test_execution_package_installs_and_reuses_verified_artifacts(tmp_path,monkeypatch):
    producer=tmp_path/"producer";source=build(producer)
    archive=runtime.package(producer,tmp_path/"release")
    consumer=tmp_path/"consumer";config=consumer/"native/sandbox/release.json";config.parent.mkdir(parents=True)
    shutil.copyfile(producer/"native/sandbox/release.json",config)
    calls=[]
    def download(url,path):
        calls.append(url);shutil.copyfile(archive.with_name(url.rsplit("/",1)[1]),path)
    monkeypatch.setattr(runtime.delivery,"download",download)
    installed=runtime.install(consumer)
    assert installed.read_bytes()==(source/installed.name).read_bytes()
    assert len(calls)==2
    assert runtime.install(consumer)==installed
    assert len(calls)==2
    installed.with_name("redpanda-hook.dll").write_bytes(b"changed")
    with pytest.raises(ValueError):
        runtime.install(consumer)
    assert len(calls)==2


def test_package_requires_hook_and_license(tmp_path):
    source=build(tmp_path)
    (source/"redpanda-hook.dll").unlink()
    with pytest.raises(ValueError):
        runtime.package(tmp_path,tmp_path/"release")
