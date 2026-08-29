"""Silence-based auto-stop.

Measures CONTINUOUS silence since the last frame above the speech threshold, so
a long dictation with natural pauses is never cut off mid-sentence, but a
forgotten session cannot record all afternoon.

Applies to hands-free (toggle) sessions only. In hold mode the user's finger is
the stop condition and silence is irrelevant.
"""


class SilenceMonitor:
    def __init__(self, threshold: float = 0.012, stop_after_s: float = 90.0):
        self.threshold = threshold
        self.stop_after_s = stop_after_s
        self.silent_for = 0.0

    def feed(self, level: float, dt: float) -> bool:
        """Returns True once continuous silence has reached the limit."""
        if level > self.threshold:
            self.silent_for = 0.0
            return False
        self.silent_for += dt
        return self.silent_for >= self.stop_after_s

    def reset(self) -> None:
        self.silent_for = 0.0
