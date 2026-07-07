import os

import pytest

_TEST_DIR = os.path.dirname(os.path.abspath(__file__))


@pytest.fixture(autouse=True, scope="session")
def _run_from_test_dir():
    """Ensure tests run with cwd == test/ regardless of how pytest was invoked.

    Several tests reference fixture files using paths relative to this
    directory (e.g. "../resources/CuteLogix.ACD"). Running `pytest` from the
    repository root (the normal, documented way to run the suite) would
    otherwise resolve those paths one level above the repo entirely.
    """
    original_cwd = os.getcwd()
    os.chdir(_TEST_DIR)
    try:
        yield
    finally:
        os.chdir(original_cwd)
