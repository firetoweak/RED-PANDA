"""验证入口不允许个人配置改变测试范围。"""
from scripts.ci import verification_environment


def test_verification_environment_excludes_inherited_test_and_product_settings(monkeypatch):
    for name in ("KONDEN_HOME", "KONDEN_CONFIG", "KONDEN_RUN_LIVE_TESTS",
                 "PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTHONPATH"):
        monkeypatch.setenv(name, "inherited")
    monkeypatch.setenv("PYTHONUTF8", "0")
    monkeypatch.setenv("PYTHONIOENCODING", "cp1252")
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "0")
    environment = verification_environment()
    assert not any(name.startswith("KONDEN_") for name in environment)
    assert not {"PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTHONPATH"} & environment.keys()
    assert environment["PYTHONUTF8"] == "1"
    assert environment["PYTHONIOENCODING"] == "utf-8"
    assert environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    assert environment["PATH"]
    assert verification_environment() == environment
