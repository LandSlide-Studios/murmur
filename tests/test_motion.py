from murmur.ui.motion import Spring, ease_out_expo


def settle(s, frames=400, dt=1 / 60):
    for _ in range(frames):
        s.step(dt)
    return s.value


def test_spring_converges_to_target():
    s = Spring(value=0.0)
    s.target = 1.0
    assert abs(settle(s) - 1.0) < 0.01


def test_spring_converges_downward_too():
    s = Spring(value=1.0)
    s.target = 0.0
    assert abs(settle(s)) < 0.01


def test_retargeting_midflight_does_not_snap():
    """An interrupted transition must blend, not jump. This is the whole
    difference between fancy and janky."""
    s = Spring(value=0.0)
    s.target = 1.0
    for _ in range(10):
        s.step(1 / 60)
    midflight = s.value
    assert 0.0 < midflight < 1.0
    s.target = 0.0
    s.step(1 / 60)
    assert s.value != 0.0                       # did not teleport to the new target
    assert abs(s.value - midflight) < 0.2       # moved continuously


def test_velocity_is_preserved_across_a_retarget():
    s = Spring(value=0.0)
    s.target = 1.0
    for _ in range(8):
        s.step(1 / 60)
    v = s.velocity
    s.target = 0.5
    assert abs(s.velocity - v) < 1e-9           # retarget alone changes no velocity


def test_at_rest_is_false_while_moving_and_true_once_settled():
    s = Spring(value=0.0)
    s.target = 1.0
    s.step(1 / 60)
    assert s.at_rest is False
    settle(s)
    assert s.at_rest is True


def test_a_stalled_frame_cannot_explode_the_spring():
    """dt is clamped: a 2-second hitch would otherwise integrate to infinity."""
    s = Spring(value=0.0)
    s.target = 1.0
    s.step(2.0)
    assert -5.0 < s.value < 5.0
    assert settle(s) ==  __import__("pytest").approx(1.0, abs=0.01)


def test_spring_starting_at_target_is_immediately_at_rest():
    s = Spring(value=0.5)
    s.target = 0.5
    assert s.at_rest is True


def test_ease_out_expo_endpoints_and_monotonicity():
    assert ease_out_expo(0.0) == 0.0
    assert ease_out_expo(1.0) == 1.0
    vals = [ease_out_expo(i / 20) for i in range(21)]
    assert all(b >= a for a, b in zip(vals, vals[1:]))
    assert ease_out_expo(0.5) > 0.5             # front-loaded, as an arrival curve
