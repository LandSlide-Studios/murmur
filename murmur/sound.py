"""Audio cues for the start and end of a dictation.

`winsound.PlaySound` with SND_ASYNC, because it returns immediately. Anything
blocking here would run on the hook or worker thread and delay the recording
itself.

SND_NOSTOP is deliberately NOT set: a new cue should cut off the previous one
rather than be dropped, so rapid dictations stay in sync with what you hear.
"""

import logging
import sys
import wave
from pathlib import Path

log = logging.getLogger(__name__)

ASSETS = Path(__file__).resolve().parent.parent / "assets"
CUES = ("start", "done", "cancel")


class Sounds:
    def __init__(self, enabled: bool = True, assets_dir: Path | None = None):
        self.enabled = enabled
        self._dir = Path(assets_dir) if assets_dir else ASSETS
        self._paths: dict[str, str] = {}
        self._durations: dict[str, int] = {}
        self._winsound = None
        if not enabled or sys.platform != "win32":
            return
        try:
            import winsound

            self._winsound = winsound
        except ImportError:
            log.info("winsound unavailable; audio cues disabled")
            self.enabled = False
            return
        for cue in CUES:
            path = self._dir / f"{cue}.wav"
            if path.exists():
                self._paths[cue] = str(path)
                self._durations[cue] = self._duration_ms(path)
            else:
                log.warning("missing audio cue %s", path.name)

    @staticmethod
    def _duration_ms(path: Path) -> int:
        try:
            with wave.open(str(path), "rb") as w:
                return int(w.getnframes() / w.getframerate() * 1000)
        except Exception:
            return 150

    def duration_ms(self, cue: str) -> int:
        """How long the cue plays. The recorder mutes for exactly this long so
        the speakers are not recorded back through the microphone."""
        return self._durations.get(cue, 0)

    def play(self, cue: str) -> None:
        """Fire and forget. A missing or broken cue is never worth an error."""
        if not self.enabled or self._winsound is None:
            return
        path = self._paths.get(cue)
        if not path:
            return
        try:
            self._winsound.PlaySound(
                path,
                self._winsound.SND_FILENAME
                | self._winsound.SND_ASYNC
                | self._winsound.SND_NODEFAULT,
            )
        except Exception:
            log.debug("could not play the %s cue", cue, exc_info=True)

    def stop(self) -> None:
        if self._winsound is not None:
            try:
                self._winsound.PlaySound(None, self._winsound.SND_PURGE)
            except Exception:
                pass
