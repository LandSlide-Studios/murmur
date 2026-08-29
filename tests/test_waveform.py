from murmur.ui.waveform import FLOOR, BarModel


def drive(m, level, frames=40, dt=1 / 60):
    for _ in range(frames):
        m.step(level=level, dt=dt)
    return m.heights()


def test_bars_rise_toward_a_loud_level():
    assert max(drive(BarModel(n=5), 0.5)) > 0.2


def test_bars_settle_back_to_the_floor_on_silence():
    m = BarModel(n=5)
    drive(m, 0.5)
    assert max(drive(m, 0.0, frames=400)) < 0.15


def test_bars_are_not_all_identical_so_it_reads_as_a_wave():
    h = drive(BarModel(n=5), 0.4, frames=20)
    assert len({round(x, 3) for x in h}) > 1


def test_heights_stay_in_unit_range_under_a_loud_spike():
    assert all(0.0 <= x <= 1.0 for x in drive(BarModel(n=5), 10.0, frames=60))


def test_heights_never_go_below_zero_on_a_negative_level():
    assert all(x >= 0.0 for x in drive(BarModel(n=5), -1.0, frames=60))


def test_bar_count_is_respected():
    assert len(BarModel(n=7).heights()) == 7


def test_bars_start_at_the_floor_not_at_zero():
    """A pill whose bars start flat looks dead for the first frames."""
    assert all(abs(x - FLOOR) < 1e-6 for x in BarModel(n=5).heights())


def test_breathe_keeps_bars_near_the_floor_and_moving():
    m = BarModel(n=5)
    seen = set()
    for _ in range(120):
        m.breathe(dt=1 / 60)
        seen.add(round(max(m.heights()), 4))
    assert max(seen) < 0.2
    assert len(seen) > 1                       # actually animating, not frozen


def test_louder_input_produces_taller_bars_than_quieter():
    quiet = max(drive(BarModel(n=5), 0.05, frames=60))
    loud = max(drive(BarModel(n=5), 0.6, frames=60))
    assert loud > quiet


def test_zero_dt_does_not_move_the_bars():
    m = BarModel(n=5)
    before = m.heights()
    for _ in range(10):
        m.step(level=0.9, dt=0.0)
    assert m.heights() == before
