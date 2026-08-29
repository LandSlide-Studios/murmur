import numpy as np
import pytest

from murmur.platform.win import autostart
from murmur.stt.cloud import CloudTranscriber, to_wav_bytes


# --- autostart ---------------------------------------------------------------

def test_enable_then_disable_roundtrips():
    was = autostart.is_enabled()
    try:
        assert autostart.set_enabled(
            True, command='"C:/fake/pythonw.exe" -m murmur')
        assert autostart.is_enabled() is True
        assert autostart.set_enabled(False)
        assert autostart.is_enabled() is False
    finally:
        autostart.set_enabled(was)


def test_disable_when_absent_is_not_an_error():
    was = autostart.is_enabled()
    try:
        autostart.set_enabled(False)
        assert autostart.set_enabled(False) is True
    finally:
        autostart.set_enabled(was)


def test_default_command_uses_pythonw_to_avoid_a_console_window():
    assert "pythonw" in autostart.default_command().lower()


# --- cloud backend -----------------------------------------------------------

def test_missing_key_raises_at_construction(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
        CloudTranscriber(provider="groq")


def test_unknown_provider_is_rejected():
    with pytest.raises(ValueError):
        CloudTranscriber(provider="nope")


def test_wav_encoding_produces_a_riff_header():
    b = to_wav_bytes(np.zeros(16000, dtype=np.float32), 16000)
    assert b[:4] == b"RIFF" and b[8:12] == b"WAVE"


def test_wav_encoding_clips_rather_than_wrapping():
    loud = np.full(100, 5.0, dtype=np.float32)
    pcm = np.frombuffer(to_wav_bytes(loud, 16000)[44:], dtype=np.int16)
    assert pcm.max() > 32000 and pcm.min() > 0     # clipped high, not wrapped negative


def test_cloud_is_never_selected_automatically():
    """The offline guarantee is the default; cloud must be opt-out, not a fallback."""
    from murmur.config import DEFAULTS
    assert DEFAULTS["stt"]["backend"] == "local"


# --- regression: the Run key entry must work with no working directory ---

def test_autostart_command_does_not_depend_on_the_working_directory():
    """A Run key entry has no CWD. '-m murmur' died with 'No module named
    murmur' at every login, invisibly, because pythonw has no console."""
    cmd = autostart.default_command()
    assert "-m murmur" not in cmd
    assert "run_murmur.pyw" in cmd
    assert "pythonw" in cmd.lower()


def test_autostart_launcher_exists_at_the_path_it_names():
    import re
    from pathlib import Path

    paths = re.findall(r'"([^"]+)"', autostart.default_command())
    launcher = Path(paths[-1])
    assert launcher.is_absolute()
    assert launcher.exists(), f"{launcher} does not exist"


def test_the_launcher_can_import_murmur_from_any_directory(tmp_path):
    """The whole point of the launcher: it puts the project root on sys.path so
    the import works regardless of where Windows starts it. Run from a foreign
    working directory, which is exactly the login case that used to fail."""
    import subprocess
    import sys as _sys

    root = autostart.project_root()
    code = (
        "import sys\n"
        f"sys.path.insert(0, r'{root}')\n"
        "import murmur.config\n"
        "print('IMPORT-OK')\n"
    )
    out = subprocess.run([_sys.executable, "-c", code], cwd=str(tmp_path),
                         capture_output=True, text=True)
    assert "IMPORT-OK" in out.stdout, out.stderr


def test_module_form_would_have_failed_from_a_foreign_directory(tmp_path):
    """Documents the bug: this is what the Run key used to contain."""
    import subprocess
    import sys as _sys

    out = subprocess.run([_sys.executable, "-c", "import murmur"],
                         cwd=str(tmp_path), capture_output=True, text=True)
    assert out.returncode != 0
    assert "No module named" in out.stderr
