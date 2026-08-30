"""Silence-based auto-stop.

Measures CONTINUOUS silence since the last frame above the speech threshold, so
a long dictation with natural pauses is never cut off mid-sentence, but a
forgotten session cannot record all afternoon.

Applies to hands-free (toggle) sessions only. In hold mode the user's finger is
the stop condition and silence is irrelevant.
"""


class SilenceMonitor:
    def __init__(self, threshold: float = 0.012, stop_after_s: float = 90.0,
                 reset_after_s: float = 0.30):
        self.threshold = threshold
        self.stop_after_s = stop_after_s
        # How much CONTINUOUS sound it takes to call the session live again.
        # One frame above the threshold used to do it, so a door or a cough
        # once every ninety seconds kept a forgotten session recording forever.
        # Short enough that a syllable counts; long enough that a click does not.
        self.reset_after_s = reset_after_s
        self.silent_for = 0.0
        self.loud_for = 0.0

    def feed(self, level: float, dt: float) -> bool:
        """Returns True once continuous silence has reached the limit."""
        if level > self.threshold:
            # Sustained sound, not one frame. A single block above the threshold
            # used to reset the whole counter, so a door, a cough or an HVAC
            # cycle once every ninety seconds kept a forgotten session alive
            # indefinitely -- and this module promises the opposite.
            self.loud_for += dt
            if self.loud_for >= self.reset_after_s:
                self.silent_for = 0.0
            return False
        self.loud_for = 0.0
        self.silent_for += dt
        return self.silent_for >= self.stop_after_s

    def reset(self) -> None:
        self.silent_for = 0.0
