"""Optional cloud transcription.

Only reachable when stt.backend is explicitly "cloud". It is never selected
automatically and is never a fallback: the offline guarantee is the default and
must stay opt-out, not opt-in.
"""

import io
import logging
import os
import wave

import numpy as np

log = logging.getLogger(__name__)

_ENV = {"groq": "GROQ_API_KEY", "openai": "OPENAI_API_KEY"}
_MODEL = {"groq": "whisper-large-v3", "openai": "whisper-1"}


def to_wav_bytes(pcm: np.ndarray, sample_rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes((np.clip(pcm, -1.0, 1.0) * 32767).astype(np.int16).tobytes())
    return buf.getvalue()


class CloudTranscriber:
    def __init__(self, provider: str = "groq", model: str | None = None,
                 sample_rate: int = 16000, language: str = "en"):
        if provider not in _ENV:
            raise ValueError(f"unknown cloud provider {provider!r}")
        self.provider = provider
        self.sample_rate = sample_rate
        self.language = language

        env_name = _ENV[provider]
        self.key = os.environ.get(env_name)
        if not self.key:
            # Loaded lazily so a .env beside the app works without importing
            # dotenv at module scope.
            try:
                from dotenv import load_dotenv

                load_dotenv()
                self.key = os.environ.get(env_name)
            except Exception:
                pass
        if not self.key:
            raise RuntimeError(
                f"{env_name} is not set; cannot use the cloud STT backend")
        self.model = model or _MODEL[provider]
        log.info("STT: cloud %s (%s)", provider, self.model)

    def transcribe(self, pcm: np.ndarray, hotwords: list[str]) -> str:
        if pcm is None or len(pcm) == 0:
            return ""
        wav = to_wav_bytes(pcm, self.sample_rate)
        if self.provider == "groq":
            from groq import Groq

            client = Groq(api_key=self.key)
        else:
            from openai import OpenAI

            client = OpenAI(api_key=self.key)

        kwargs = {
            "file": ("audio.wav", wav),
            "model": self.model,
            "language": self.language,
        }
        if hotwords:
            kwargs["prompt"] = " ".join(hotwords)
        return client.audio.transcriptions.create(**kwargs).text.strip()
