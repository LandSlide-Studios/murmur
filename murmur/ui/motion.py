"""Spring physics for the overlay.

One integrator drives every animated property. State changes RETARGET the
springs instead of starting a new animation, so an interrupted transition
blends into the next one rather than snapping. That continuity is the whole
difference between motion that feels designed and motion that feels janky.
"""

_MAX_DT = 1 / 30.0          # a stalled frame must not integrate to infinity


class Spring:
    __slots__ = ("value", "velocity", "_target", "stiffness", "damping",
                 "epsilon")

    def __init__(
        self,
        value: float = 0.0,
        stiffness: float = 180.0,
        damping: float = 26.0,
        epsilon: float = 0.001,
    ):
        self.value = value
        self.velocity = 0.0
        self._target = value
        self.stiffness = stiffness
        self.damping = damping
        self.epsilon = epsilon

    @property
    def target(self) -> float:
        return self._target

    @target.setter
    def target(self, value: float) -> None:
        """Refuse a non-finite target.

        One NaN written here used to be permanent: the integrator carried it
        into `value` and `velocity`, retargeting to a real number never restored
        them, and `at_rest` was False forever -- so the overlay's hide condition
        could never fire and the comet raised outright on the next launch.
        """
        v = float(value)
        if v != v or v in (float("inf"), float("-inf")):
            return
        self._target = v

    def step(self, dt: float) -> float:
        dt = min(dt, _MAX_DT)
        if dt <= 0:
            return self.value
        accel = (self.stiffness * (self.target - self.value)
                 - self.damping * self.velocity)
        self.velocity += accel * dt
        self.value += self.velocity * dt
        return self.value

    def snap_to(self, value: float) -> None:
        """Jump without animating — for the first frame of a fresh appearance."""
        self.value = self.target = value
        self.velocity = 0.0

    @property
    def at_rest(self) -> bool:
        return (abs(self.value - self.target) < self.epsilon
                and abs(self.velocity) < self.epsilon)


def ease_out_expo(t: float) -> float:
    """cubic-bezier(0.16, 1, 0.3, 1) in spirit — the arrival curve harvested
    from the design library. Fast out of the gate, long gentle settle."""
    if t <= 0.0:
        return 0.0
    if t >= 1.0:
        return 1.0
    return 1.0 - pow(2.0, -10.0 * t)
