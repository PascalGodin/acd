import os

import pytest

_TEST_DIR = os.path.dirname(os.path.abspath(__file__))


@pytest.fixture(autouse=True)
def _reset_log_once_dedup():
    """Clear `_log_once()`'s module-level "already warned this process"
    cache (`acd.l5x.export_l5x._WARNED_ONCE_MESSAGES`) before every test.

    `_log_once()` is a real, deliberately process-lifetime cache (see its
    own docstring) -- but a whole `pytest` run is one process, so without
    this, a test earlier in the run could "use up" a warning message a
    later test also expects to see at WARNING level, silently downgrading
    it to DEBUG (invisible under the default WARNING-level test sink) and
    producing a flaky, run-order-dependent failure that has nothing to do
    with the actual code under test.
    """
    import acd.l5x.export_l5x as _export_l5x_module
    _export_l5x_module._WARNED_ONCE_MESSAGES.clear()
    yield
    _export_l5x_module._WARNED_ONCE_MESSAGES.clear()


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
