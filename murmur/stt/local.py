"""Local speech-to-text via faster-whisper (CTranslate2).

Device selection is measured, not assumed, and the measurement has a trap in it.

CTranslate2 loads cuBLAS lazily on the first matrix multiply, so a probe that
only CONSTRUCTS a CUDA model returns True on a machine where every real
transcription then fails. The probe here runs an actual inference on non-silent
audio with VAD off — silence would be filtered away, zero segments encoded, no
GPU math performed, and the probe would be a false positive again.

Falling back silently to CPU is correct. Claiming a GPU we never actually used
is not.
"""


import logging
import os
import sys
import threading
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

_CUDA_COMPUTE = "int8_float16"
_CPU_COMPUTE = "int8"


def _add_cuda_dll_dirs() -> list[str]:
    """Put the pip-installed NVIDIA runtime DLLs where Windows will find them.

    nvidia-cublas-cu12 and nvidia-cudnn-cu12 drop their DLLs under
    site-packages/nvidia/*/bin, which is not on the loader path by default.

    Both mechanisms are required and neither is sufficient alone:
      * os.add_dll_directory covers loads that pass LOAD_LIBRARY_SEARCH_USER_DIRS.
      * PATH covers CTranslate2's lazy cuBLAS load, which uses a plain
        LoadLibrary that ignores the user-dirs list. Without this, the model
        CONSTRUCTS fine on CUDA and then dies on the first matrix multiply.
    """
    if sys.platform != "win32":
        return []
    added: list[str] = []
    for entry in sys.path:
        # sys.path[0] is '' (the CWD) for `python -m murmur`. A directory named
        # nvidia/ sitting in whatever folder the app was launched from must not
        # shadow the real site-packages one, so skip empty/relative entries and
        # never stop at the first hit.
        if not entry or not os.path.isabs(entry):
            continue
        nvidia = Path(entry) / "nvidia"
        if not nvidia.is_dir():
            continue
        for binf in sorted(nvidia.glob("*/bin")):
            if not binf.is_dir():
                continue
            try:
                os.add_dll_directory(str(binf))
            except (OSError, AttributeError):
                # Only record what was actually registered, or the log and the
                # return value claim a directory the loader rejected.
                continue
            added.append(str(binf))
    if added:
        current = os.environ.get("PATH", "")
        existing = {p.rstrip("\\/").lower() for p in current.split(os.pathsep) if p}
        missing = [d for d in added if d.rstrip("\\/").lower() not in existing]
        if missing:
            os.environ["PATH"] = os.pathsep.join(missing) + os.pathsep + current
        log.debug("registered %d CUDA DLL directories", len(added))
    return added


def _load_whisper_model(model: str, device: str, compute_type: str):
    """Indirection so the CUDA probe can be stubbed in tests.

    Tries the local cache first. faster-whisper otherwise contacts HuggingFace on
    every single load to check for a newer revision — which breaks the offline
    guarantee and adds latency to app start even when the model is already on
    disk. Only if the model is genuinely not cached do we allow a download.
    """
    from faster_whisper import WhisperModel

    try:
        return WhisperModel(
            model, device=device, compute_type=compute_type, local_files_only=True
        )
    except Exception as e:
        log.info("model %r not in local cache, downloading once: %s", model, e)
        return WhisperModel(model, device=device, compute_type=compute_type)


_probe_lock = threading.Lock()
_probe_result: bool | None = None


def _cuda_works() -> bool:
    """Run a real transcription on CUDA. Cached: the probe costs a model load.

    Constructing the model is NOT a sufficient test. CTranslate2 loads cuBLAS
    lazily on the first matrix multiply, so a construction-only probe returns
    True on a machine where every actual transcription then fails. The probe
    therefore encodes real audio and forces the segment generator.

    The signal must be non-silent and VAD must be off, or the filter removes
    every frame, zero segments are encoded, no GPU math runs, and the probe is
    a false positive again.

    Double-checked under a lock rather than lru_cache: lru_cache protects its
    dict, not the wrapped call, so concurrent first-callers would each run a
    full model load plus a GPU inference on an 8GB card.
    """
    global _probe_result
    if _probe_result is not None:
        return _probe_result
    with _probe_lock:
        if _probe_result is None:
            _probe_result = _probe_cuda()
        return _probe_result


def _reset_cuda_probe() -> None:
    """Test hook. Production never re-probes within a process."""
    global _probe_result
    _probe_result = None


_cuda_works.cache_clear = _reset_cuda_probe   # keep the lru_cache-era call site


def _probe_cuda() -> bool:
    _add_cuda_dll_dirs()
    try:
        model = _load_whisper_model("tiny", "cuda", _CUDA_COMPUTE)
        rng = np.random.default_rng(0)
        noise = (rng.standard_normal(16000) * 0.05).astype(np.float32)
        segments, _ = model.transcribe(noise, language="en", beam_size=1,
                                       vad_filter=False)
        list(segments)          # generator is lazy; this is what runs the GPU
        log.info("CUDA verified for faster-whisper (real inference)")
        return True
    except Exception as e:
        log.warning("CUDA unusable for faster-whisper, falling back to CPU: %s", e)
        return False


def pick_device(pref: str) -> tuple[str, str]:
    """Returns (device, compute_type). 'auto' probes; explicit values are obeyed."""
    if pref == "cpu":
        return ("cpu", _CPU_COMPUTE)
    if pref == "cuda":
        return ("cuda", _CUDA_COMPUTE)
    return ("cuda", _CUDA_COMPUTE) if _cuda_works() else ("cpu", _CPU_COMPUTE)


class LocalTranscriber:
    def __init__(
        self,
        model: str = "large-v3-turbo",
        device: str = "auto",
        language: str = "en",
    ):
        self.device, self.compute_type = pick_device(device)
        self.language = language
        self._model_name = model
        _add_cuda_dll_dirs()
        try:
            self.model = _load_whisper_model(model, self.device, self.compute_type)
        except Exception:
            if self.device != "cuda":
                raise
            # An explicit device="cuda" must not kill a tray app with no console.
            log.exception("CUDA model load failed; falling back to CPU")
            self.device, self.compute_type = "cpu", _CPU_COMPUTE
            self.model = _load_whisper_model(model, self.device, self.compute_type)
        log.info("STT ready: %s on %s (%s)", model, self.device, self.compute_type)

    def _run(self, pcm: np.ndarray, hotwords: list[str]) -> str:
        kwargs = {
            "language": self.language,
            "beam_size": 1,
            "vad_filter": True,
        }
        if hotwords:
            kwargs["hotwords"] = " ".join(hotwords)
        segments, _info = self.model.transcribe(pcm, **kwargs)
        return " ".join(s.text.strip() for s in segments).strip()

    def transcribe(self, pcm: np.ndarray, hotwords: list[str]) -> str:
        if pcm is None or len(pcm) == 0:
            return ""
        try:
            return self._run(pcm, hotwords)
        except Exception:
            if self.device != "cuda":
                raise
            # VRAM is shared with the polish model and the desktop compositor.
            # A GPU failure mid-session must not cost the user their dictation:
            # rebuild on CPU and retry once, permanently, for this instance.
            log.exception("CUDA transcription failed; retrying once on CPU")
            self.device, self.compute_type = "cpu", _CPU_COMPUTE
            self.model = _load_whisper_model(
                self._model_name, self.device, self.compute_type
            )
            return self._run(pcm, hotwords)
