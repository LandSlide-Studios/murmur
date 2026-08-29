import numpy as np
import pytest

from murmur.stt import local as L


def test_pick_device_honours_explicit_cpu():
    assert L.pick_device("cpu") == ("cpu", "int8")


def test_pick_device_honours_explicit_cuda_without_probing(monkeypatch):
    monkeypatch.setattr(L, "_cuda_works", lambda: pytest.fail("should not probe"))
    assert L.pick_device("cuda") == ("cuda", "int8_float16")


def test_pick_device_auto_uses_cuda_when_available(monkeypatch):
    monkeypatch.setattr(L, "_cuda_works", lambda: True)
    assert L.pick_device("auto") == ("cuda", "int8_float16")


def test_pick_device_auto_falls_back_to_cpu_when_cuda_unavailable(monkeypatch):
    monkeypatch.setattr(L, "_cuda_works", lambda: False)
    assert L.pick_device("auto") == ("cpu", "int8")


def test_cuda_probe_swallows_failure_and_returns_false(monkeypatch):
    """A missing cublas DLL must degrade to CPU, never crash the app."""
    class Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("Library cublas64_12.dll is not found")

    monkeypatch.setattr(L, "_load_whisper_model", Boom)
    L._cuda_works.cache_clear()
    assert L._cuda_works() is False
    L._cuda_works.cache_clear()


def test_cuda_probe_is_cached_so_it_runs_once(monkeypatch):
    calls = []

    def counted(*a, **k):
        calls.append(1)
        return object()

    monkeypatch.setattr(L, "_load_whisper_model", counted)
    L._cuda_works.cache_clear()
    L._cuda_works()
    L._cuda_works()
    assert len(calls) == 1
    L._cuda_works.cache_clear()


def test_transcribe_joins_segments_and_strips(monkeypatch):
    class Seg:
        def __init__(self, t):
            self.text = t

    class FakeModel:
        def transcribe(self, pcm, **kw):
            return iter([Seg("  Hello there. "), Seg(" Second part.")]), None

    t = L.LocalTranscriber.__new__(L.LocalTranscriber)
    t.model = FakeModel()
    t.language = "en"
    assert t.transcribe(np.zeros(16000, dtype=np.float32), hotwords=[]) == \
        "Hello there. Second part."


def test_transcribe_passes_hotwords_through(monkeypatch):
    seen = {}

    class FakeModel:
        def transcribe(self, pcm, **kw):
            seen.update(kw)
            return iter([]), None

    t = L.LocalTranscriber.__new__(L.LocalTranscriber)
    t.model = FakeModel()
    t.language = "en"
    t.transcribe(np.zeros(10, dtype=np.float32), hotwords=["Halvorsen", "Landslide"])
    assert "Halvorsen" in seen.get("hotwords", "")


def test_transcribe_omits_hotwords_when_empty():
    seen = {}

    class FakeModel:
        def transcribe(self, pcm, **kw):
            seen.update(kw)
            return iter([]), None

    t = L.LocalTranscriber.__new__(L.LocalTranscriber)
    t.model = FakeModel()
    t.language = "en"
    t.transcribe(np.zeros(10, dtype=np.float32), hotwords=[])
    assert "hotwords" not in seen


def test_empty_audio_returns_empty_string_without_calling_model():
    class Explode:
        def transcribe(self, *a, **k):
            raise AssertionError("model called on empty audio")

    t = L.LocalTranscriber.__new__(L.LocalTranscriber)
    t.model = Explode()
    t.language = "en"
    assert t.transcribe(np.zeros(0, dtype=np.float32), hotwords=[]) == ""


# --- regression guards for the lazy-cuBLAS false positive ---

def test_cuda_probe_runs_a_real_inference_not_just_construction(monkeypatch):
    """Constructing a CUDA model is not proof it works: cuBLAS loads lazily on
    the first matmul. The probe must consume the segment generator."""
    consumed = {"yes": False}

    class Model:
        def transcribe(self, pcm, **kw):
            assert kw.get("vad_filter") is False, "VAD would strip the probe signal"
            assert pcm.std() > 0, "probe signal must not be silence"

            def gen():
                consumed["yes"] = True
                return iter(())

            return gen(), None

    monkeypatch.setattr(L, "_load_whisper_model", lambda *a, **k: Model())
    L._cuda_works.cache_clear()
    assert L._cuda_works() is True
    assert consumed["yes"], "probe did not force the generator"
    L._cuda_works.cache_clear()


def test_cuda_probe_returns_false_when_inference_raises(monkeypatch):
    class Model:
        def transcribe(self, pcm, **kw):
            raise RuntimeError("Library cublas64_12.dll is not found")

    monkeypatch.setattr(L, "_load_whisper_model", lambda *a, **k: Model())
    L._cuda_works.cache_clear()
    assert L._cuda_works() is False
    L._cuda_works.cache_clear()


def test_gpu_failure_midsession_falls_back_to_cpu_and_keeps_the_words(monkeypatch):
    """VRAM is shared with the polish model. A GPU OOM mid-session must not
    cost the user their dictation."""
    class Seg:
        text = "recovered on cpu"

    class CpuModel:
        def transcribe(self, pcm, **kw):
            return iter([Seg()]), None

    class GpuModel:
        def transcribe(self, pcm, **kw):
            raise RuntimeError("CUDA out of memory")

    monkeypatch.setattr(L, "_load_whisper_model", lambda *a, **k: CpuModel())
    t = L.LocalTranscriber.__new__(L.LocalTranscriber)
    t.model, t.device, t.language, t._model_name = GpuModel(), "cuda", "en", "small"

    assert t.transcribe(np.zeros(16000, dtype=np.float32), hotwords=[]) == \
        "recovered on cpu"
    assert t.device == "cpu"


def test_cpu_failure_is_not_retried_and_propagates():
    class Boom:
        def transcribe(self, pcm, **kw):
            raise RuntimeError("genuinely broken")

    t = L.LocalTranscriber.__new__(L.LocalTranscriber)
    t.model, t.device, t.language, t._model_name = Boom(), "cpu", "en", "small"
    try:
        t.transcribe(np.zeros(16000, dtype=np.float32), hotwords=[])
    except RuntimeError as e:
        assert "genuinely broken" in str(e)
    else:
        raise AssertionError("should have propagated")


# --- I4: a decoy nvidia/ dir must not shadow the real site-packages one ---

def test_dll_scan_skips_relative_sys_path_entries(monkeypatch, tmp_path):
    """sys.path[0] is '' (the CWD) under `python -m murmur`. An nvidia/ folder
    in the launch directory previously won and disabled CUDA entirely."""
    decoy = tmp_path / "cwd"
    (decoy / "nvidia" / "decoy" / "bin").mkdir(parents=True)
    real = tmp_path / "site-packages"
    (real / "nvidia" / "cublas" / "bin").mkdir(parents=True)

    monkeypatch.chdir(decoy)
    monkeypatch.setattr(L.sys, "path", ["", "relative/path", str(real)])
    monkeypatch.setattr(L.os, "add_dll_directory", lambda d: None)
    monkeypatch.setattr(L.sys, "platform", "win32")
    added = L._add_cuda_dll_dirs()
    assert any("cublas" in a for a in added)
    assert not any("decoy" in a for a in added)


def test_dll_scan_does_not_stop_at_the_first_hit(monkeypatch, tmp_path):
    a = tmp_path / "a"
    (a / "nvidia" / "cudnn" / "bin").mkdir(parents=True)
    b = tmp_path / "b"
    (b / "nvidia" / "cublas" / "bin").mkdir(parents=True)
    monkeypatch.setattr(L.sys, "path", [str(a), str(b)])
    monkeypatch.setattr(L.os, "add_dll_directory", lambda d: None)
    monkeypatch.setattr(L.sys, "platform", "win32")
    added = L._add_cuda_dll_dirs()
    assert any("cudnn" in x for x in added) and any("cublas" in x for x in added)


def test_dll_scan_does_not_report_directories_the_loader_rejected(monkeypatch, tmp_path):
    site = tmp_path / "site-packages"
    (site / "nvidia" / "cublas" / "bin").mkdir(parents=True)
    monkeypatch.setattr(L.sys, "path", [str(site)])
    monkeypatch.setattr(L.sys, "platform", "win32")

    def refuse(d):
        raise OSError("rejected")

    monkeypatch.setattr(L.os, "add_dll_directory", refuse)
    assert L._add_cuda_dll_dirs() == []


# --- I3: an explicit device="cuda" must degrade, not kill the app ---

def test_explicit_cuda_load_failure_falls_back_to_cpu(monkeypatch):
    calls = []

    def loader(model, device, ctype):
        calls.append(device)
        if device == "cuda":
            raise RuntimeError("Library cublas64_12.dll is not found")
        return object()

    monkeypatch.setattr(L, "_load_whisper_model", loader)
    t = L.LocalTranscriber(model="small", device="cuda")
    assert t.device == "cpu"
    assert calls == ["cuda", "cpu"]


def test_explicit_cpu_load_failure_still_raises(monkeypatch):
    def loader(model, device, ctype):
        raise RuntimeError("model file corrupt")

    monkeypatch.setattr(L, "_load_whisper_model", loader)
    with pytest.raises(RuntimeError, match="corrupt"):
        L.LocalTranscriber(model="small", device="cpu")


def test_real_constructor_sets_device_and_compute_type(monkeypatch):
    """The suite previously only used __new__, so __init__ was never exercised
    and the explicit-cuda crash shipped unnoticed."""
    monkeypatch.setattr(L, "_load_whisper_model", lambda *a, **k: object())
    t = L.LocalTranscriber(model="small", device="cpu", language="en")
    assert (t.device, t.compute_type, t._model_name) == ("cpu", "int8", "small")


# --- I9: the probe must not run once per thread ---

def test_cuda_probe_is_serialised_across_threads(monkeypatch):
    import threading as _t

    loads = []
    barrier = _t.Barrier(4)

    class Model:
        def transcribe(self, pcm, **kw):
            return iter(()), None

    def loader(*a, **k):
        loads.append(1)
        return Model()

    monkeypatch.setattr(L, "_load_whisper_model", loader)
    L._cuda_works.cache_clear()

    def run():
        barrier.wait()
        L._cuda_works()

    threads = [_t.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(loads) == 1, f"probe ran {len(loads)} times; must be serialised"
    L._cuda_works.cache_clear()
