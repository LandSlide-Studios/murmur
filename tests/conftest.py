"""Shared fixtures.

These were previously defined per test module, so a new file could not reach
them. Modules that define their own still win — pytest resolves the nearest
definition — so nothing existing changes behaviour by this file appearing.
"""
import time

import numpy as np
import pytest

import murmur.app as A
from murmur.config import Config


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def pill(qapp):
    from murmur.ui.pill import Pill

    p = Pill(show_when_idle=True)
    yield p
    p.hide()


@pytest.fixture
def app(tmp_path, monkeypatch):
    """A MurmurApp with no microphone, no UI Automation and no live stores.

    `end()` is deliberately slowed: the real one is not instant, and the
    concurrency defects this suite guards only open when the call between
    claiming a session and returning its audio takes real time.
    """
    monkeypatch.setattr(A, "data_dir", lambda: tmp_path)
    cfg = Config.load(tmp_path / "nope.json")
    # Constructing the UI Automation reader initialises COM on the test thread,
    # which collides with other tests' worker threads.
    cfg.set("learning.uia_readback", False)
    mu = A.MurmurApp(cfg)

    mu.recorder.open = lambda: None
    mu.recorder.begin = lambda: None

    def slow_end():
        time.sleep(0.002)
        return np.zeros(16000, dtype=np.float32)

    mu.recorder.end = slow_end
    mu.recorder.close = lambda: None
    yield mu
    mu.history.close()
    mu.vocab.close()
