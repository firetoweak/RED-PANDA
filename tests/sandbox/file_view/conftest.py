import pytest


@pytest.fixture(autouse=True)
def file_view_directory(request, tmp_path):
    # These imported unittest contracts own one bounded fixture per test.
    request.instance.root = tmp_path
