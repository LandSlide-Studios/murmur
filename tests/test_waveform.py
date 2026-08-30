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


# --- how tall the bars actually get -----------------------------------------
#
# `audio.speech_rms_threshold` is 0.012, so anything at or above that is Tommy
# talking. Measured before the fix, the tallest bar reached 10.8% of the capsule
# at that level and 28.3% at a normal speaking voice. His words: "they're just
# not getting tall enough."

def _peak(level, n=15, frames=400, dt=1 / 60):
    m = BarModel(n=n)
    return max(max(m.step(level=level, dt=dt) or m.heights())
               for _ in range(frames))


def test_a_normal_speaking_voice_drives_the_meter_past_half():
    """28.3% before. A meter that never leaves the bottom third does not read
    as listening."""
    assert _peak(0.03) > 0.5


def test_quiet_speech_still_clearly_leaves_the_floor():
    """At the speech threshold itself the meter reached 10.8% — indistinguishable
    from the idle floor, so talking quietly looked like not talking."""
    assert _peak(0.012) > 0.25


def test_the_response_is_compressive_not_linear():
    """Speech is logarithmic. A linear gain spends most of its range on volumes
    nobody dictates at, which is why the useful band sat squashed at the bottom.
    Going from quiet to normal must move the meter more than going from loud to
    shouting moves it."""
    quiet_to_normal = _peak(0.03) - _peak(0.012)
    loud_to_shouting = _peak(0.15) - _peak(0.06)
    assert quiet_to_normal > loud_to_shouting


def test_shouting_still_fills_the_capsule_without_overflowing():
    assert 0.95 <= _peak(0.15) <= 1.0


def test_the_wave_no_longer_scales_every_bar_down():
    """The oscillator multiplied the level, so its 0.55 average meant even a
    clipping input averaged 40% height. The wave must modulate around a tall
    value rather than dragging the whole row down.

    Averaged over time, not sampled at one frame: a single frame catches
    whatever the travelling wave happened to be doing at that instant.
    """
    m = BarModel(n=15)
    total = 0.0
    for _ in range(400):
        m.step(level=1.0, dt=1 / 60)
        total += sum(m.heights()) / 15
    assert total / 400 > 0.5


def test_the_row_keeps_real_troughs_and_does_not_flatten_into_a_block():
    """The counterweight to the test above. Chasing average height once flattened
    the row into a solid block — tall, but no longer reading as a voice. Crests
    must stand well clear of troughs."""
    m = BarModel(n=15)
    ratio = 0.0
    for _ in range(400):
        m.step(level=0.5, dt=1 / 60)
        h = m.heights()
        ratio = max(ratio, max(h) / max(min(h), 1e-6))
    assert ratio > 2.5


# --- adapting to the voice in front of it -----------------------------------
#
# The fixed full-scale above was a guess. Tommy's mic is a webcam at arm's
# length, not a headset, and a guess about how loud that reads is exactly the
# kind of thing that makes a meter feel wrong for one person and fine for
# another. It tracks a decaying peak instead, so it fits itself to the voice.

def test_a_quiet_talker_still_gets_a_tall_meter():
    """A soft voice on a distant mic must not be stuck at the bottom just
    because the reference level was chosen for someone louder."""
    assert _peak(0.015, frames=600) > 0.5


def test_a_loud_talker_does_not_sit_pinned_and_flat():
    m = BarModel(n=15)
    for _ in range(600):
        m.step(level=0.12, dt=1 / 60)
    h = m.heights()
    assert max(h) <= 1.0
    assert max(h) / max(min(h), 1e-6) > 2.0, "adapted itself into a solid block"


def test_it_does_not_amplify_the_noise_floor():
    """His measured ambient p99 is 0.00086. Auto-levelling that up would make
    the pill look like it is hearing speech in an empty room."""
    assert _peak(0.00086, frames=900) < 0.2


def test_loudness_still_registers_within_one_adapted_state():
    """Adaptation must not erase dynamics entirely — a shout should still
    outrun a murmur before the meter has had time to re-fit."""
    m = BarModel(n=15)
    for _ in range(300):
        m.step(level=0.02, dt=1 / 60)
    settled = max(m.heights())
    for _ in range(10):
        m.step(level=0.09, dt=1 / 60)
    assert max(m.heights()) > settled
