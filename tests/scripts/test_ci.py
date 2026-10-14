"""验证入口不允许个人配置改变测试范围。"""
from scripts.ci import verification_environment


def test_verification_environment_excludes_inherited_test_and_product_settings(monkeypatch):
    for name in ("REDPANDA_HOME", "REDPANDA_CONFIG", "REDPANDA_RUN_LIVE_TESTS",
                 "REDPANDA_FILESYSTEM_EXECUTABLE", "PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTHONPATH"):
        monkeypatch.setenv(name, "inherited")
    monkeypatch.setenv("PYTHONUTF8", "0")
    monkeypatch.setenv("PYTHONIOENCODING", "cp1252")
    environment = verification_environment()
    assert not any(name.startswith("REDPANDA_") for name in environment)
    assert not {"PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTHONPATH"} & environment.keys()
    assert environment["PYTHONUTF8"] == "1"
    assert environment["PYTHONIOENCODING"] == "utf-8"
    assert environment["PATH"]
    assert verification_environment() == environment
