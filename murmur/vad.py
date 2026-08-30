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
        # Long enough that a thinking pause cannot drag the average down, short
        # enough to notice a session left running.
        self.voiced_tau_s = 30.0
        # Measured against real patterns: a list read aloud is ~26% voiced and
        # ordinary speech 60-80%, while a room with a door or a fan cycle every
        # ninety seconds is under 0.5%. Two percent sits in the gap with an
        # order of magnitude either side.
        self.min_voiced = 0.02
        self.silent_for = 0.0
        self.loud_for = 0.0
        # How much of the recent past carried sound, as a decaying average.
        #
        # Burst LENGTH is the wrong discriminator and measurement says so: real
        # words are short (a list read aloud is 0.25s on, 0.70s off) while room
        # noise can be long (a door is 350ms). Requiring a sustained burst cut
        # the list reader off at 122s and let the empty room run for an hour.
        #
        # Duty cycle separates them cleanly. Speech is voiced a quarter to four
        # fifths of the time; a room with an occasional noise is voiced well
        # under one percent.
        self.voiced = 1.0
        self.elapsed = 0.0

    def feed(self, level: float, dt: float) -> bool:
        """Returns True once continuous silence has reached the limit."""
        self.elapsed += dt
        loud = level > self.threshold
        # A decaying average of how much of the recent past carried sound.
        self.voiced += ((1.0 if loud else 0.0) - self.voiced) * min(
            1.0, dt / self.voiced_tau_s)

        if loud:
            # ANY sound clears the continuous-silence counter. Requiring a
            # sustained burst to clear it turned this into a CUMULATIVE silence
            # measure: a list read aloud banked every gap and was cut off two
            # minutes into an active dictation. Never cutting off a live
            # speaker is the older and more important of the two promises.
            self.silent_for = 0.0
            return False
        self.silent_for += dt
        # Two conditions, catching different things:
        #   silent_for -- a genuinely quiet room, the original condition
        #   voiced     -- a room that is merely NOT silent, where any transient
        #                 kept resetting the first condition forever
        # A live speaker keeps `voiced` far above the floor, so neither fires;
        # the duty-cycle test only applies once there is enough history for the
        # average to mean anything.
        return (self.silent_for >= self.stop_after_s
                or (self.elapsed >= self.stop_after_s
                    and self.voiced < self.min_voiced))

    def reset(self) -> None:
        self.silent_for = 0.0
