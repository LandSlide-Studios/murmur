from typing import Protocol

import numpy as np


class Transcriber(Protocol):
    """Both backends satisfy this. app.py depends only on this shape."""

    def transcribe(self, pcm: np.ndarray, hotwords: list[str]) -> str: ...
