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


def _worst_flips(m, level=0.5, frames=400, dt=1 / 60):
    """Sign changes in the row's first difference — i.e. how many crests it has.

    A barcode alternates up/down bar to bar and approaches n-2. A waveform has
    a few crests travelling along it.
    """
    worst = 0
    for _ in range(frames):
        m.step(level=level, dt=dt)
        h = m.heights()
        d = [h[i + 1] - h[i] for i in range(len(h) - 1)]
        worst = max(worst, sum(1 for i in range(len(d) - 1) if d[i] * d[i + 1] < 0))
    return worst


def test_the_row_forms_a_few_crests_rather_than_alternating_bar_to_bar():
    """The bars used to run on mutually non-harmonic per-bar oscillators, which
    read as alive when there were 9 of them spaced well apart. Packed to 15 bars
    at a 1.4px gap the row is visually continuous, and the same independence
    reads as a barcode. A voice has correlated neighbours."""
    assert _worst_flips(BarModel(n=15)) <= 5


def test_neighbouring_bars_stay_closer_than_distant_ones():
    m = BarModel(n=15)
    near, far = [], []
    for _ in range(400):
        m.step(level=0.5, dt=1 / 60)
        h = m.heights()
        near += [abs(h[i] - h[i + 1]) for i in range(len(h) - 1)]
        far += [abs(h[i] - h[i + 6]) for i in range(len(h) - 6)]
    assert sum(near) / len(near) < sum(far) / len(far)


def test_the_row_still_moves_rather_than_holding_one_shape():
    """Correlating neighbours must not flatten it into a static arch."""
    m = BarModel(n=15)
    shapes = set()
    for _ in range(200):
        m.step(level=0.5, dt=1 / 60)
        shapes.add(tuple(round(x, 2) for x in m.heights()))
    assert len(shapes) > 100
