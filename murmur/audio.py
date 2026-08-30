"""Microphone capture.

The stream is opened once and stays open; sessions gate the ring buffer rather
than starting and stopping the device. That is not an optimisation, it is a
correctness fix: this machine's input device takes **~550ms** to deliver its
first block after `InputStream.start()` returns. Opening per session meant a
0.4s push-to-talk captured literally zero samples, and every longer dictation
lost its opening word. Measured across MME and DirectSound, at every blocksize
and latency setting — it is device start-up cost, not a parameter mistake.

Keeping the stream open also makes `end()` instant. Closing the device from
inside the PortAudio callback (which the silence auto-stop did) took 2.0s and
froze the UI thread behind the session lock.

A short pre-roll is kept so audio from just BEFORE the chord went down is
included — people start talking as they press, not after.

Runs on the PortAudio callback thread. It never touches Qt and never blocks:
anything slow here is dropped audio.
"""

import logging
import threading
import time

import numpy as np

log = logging.getLogger(__name__)

DEFAULT_PREROLL_MS = 400

# Windows measured per chunk. 64 windows is ~25s of audio, so the transient
# stays a couple of MB whatever the recording length, while still amortising
# the Python loop over thousands of samples per iteration.
_SCAN_WINDOWS = 64


def rms(block: np.ndarray) -> float:
    if block.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(block.astype(np.float64)))))


def peak_rms(pcm: np.ndarray, sample_rate: int = 16000,
             window_ms: int = 400) -> float:
    """The loudest short window in a recording, not the average of all of it.

    This exists because the average is the wrong question. "Was this recording
    loud on average?" gets quieter the longer someone talks, because thinking
    pauses count against it. A 42.4-second dictation was thrown away as silent
    while the user was speaking through it — the longer the hold, the more
    likely the loss, which is exactly backwards.

    The right question is "is there ANY speech in here?", and that is a maximum,
    not a mean. One window over the threshold means someone spoke.
    """
    pcm = np.asarray(pcm, dtype=np.float32).ravel()
    if pcm.size == 0:
        return 0.0
    win = max(1, int(sample_rate * window_ms / 1000))
    if pcm.size <= win:
        return rms(pcm)
    n = pcm.size // win
    # Chunked, accumulating in float64 WITHOUT materialising a float64 copy of
    # the audio. This used to promote the whole clip and then square it — two
    # full-size temporaries at double width, 166MB transient for a 26MB clip,
    # immediately after read_all had done the same thing on the same path.
    #
    # nan_to_num rather than nanmax: a single NaN sample from a device fault
    # propagates and returns NaN, which compares False against every threshold,
    # so a clip of full-scale speech would be discarded as silent.
    best = 0.0
    for i in range(0, n, _SCAN_WINDOWS):
        block = pcm[i * win:min(i + _SCAN_WINDOWS, n) * win].reshape(-1, win)
        # Only pay for the cleaning copy when there is something to clean.
        # Non-finite samples come from a device fault and are vanishingly rare;
        # copying every chunk to guard against them made the common path cost
        # what the rare one does.
        if not np.isfinite(block).all():
            block = np.nan_to_num(block, nan=0.0, posinf=0.0, neginf=0.0)
        mean_square = np.einsum("ij,ij->i", block, block, dtype=np.float64) / win
        if mean_square.size:
            best = max(best, float(np.sqrt(mean_square.max())))
    tail = pcm[n * win:]
    if tail.size:
        # Measure the tail at full window width by zero-padding rather than
        # dropping it. It used to be discarded whenever it was shorter than half
        # a window, which silently threw away up to 199.9ms — and the tail is
        # the END of the recording, where the last word is. A short reply that
        # fit entirely inside it read as silence, and one extra sample flipped
        # the verdict.
        padded = np.zeros(win, dtype=np.float64)
        padded[:tail.size] = np.nan_to_num(tail.astype(np.float64))
        best = max(best, float(np.sqrt(np.square(padded).mean())))
    return 0.0 if np.isnan(best) else best


class RingBuffer:
    """Fixed-capacity float32 buffer. Drops the oldest samples when full, so a
    forgotten session consumes bounded memory instead of growing forever."""

    def __init__(self, capacity: int):
        self._buf = np.zeros(capacity, dtype=np.float32)
        self._cap = capacity
        self._len = 0
        self._start = 0
        self._lock = threading.Lock()

    def write(self, block: np.ndarray) -> None:
        block = np.asarray(block, dtype=np.float32).ravel()
        if block.size == 0 or self._cap == 0:
            return
        with self._lock:
            if block.size >= self._cap:
                self._buf[:] = block[-self._cap:]
                self._start, self._len = 0, self._cap
                return
            end = (self._start + self._len) % self._cap
            first = min(block.size, self._cap - end)
            self._buf[end:end + first] = block[:first]
            if block.size > first:
                self._buf[:block.size - first] = block[first:]
            overflow = max(0, self._len + block.size - self._cap)
            self._start = (self._start + overflow) % self._cap
            self._len = min(self._cap, self._len + block.size)

    def read_all(self) -> np.ndarray:
        """Samples in write order, as a single copy.

        This used to build an int64 index for every sample -- an arange, an add
        and a modulo, each twice the width of the audio itself -- then fancy
        index with it and call .copy() on an array fancy indexing had already
        copied. Measured on the shipped 30-minute ceiling: 235ms and 461MB
        transient for a 115MB recording, on the chord-release path, on a machine
        already holding a speech model and a 7B cleanup model on an 8GB card.

        The buffer is at most two contiguous runs. Copying them is the whole job.
        """
        with self._lock:
            if self._len == 0:
                return np.zeros(0, dtype=np.float32)
            end = self._start + self._len
            if end <= self._cap:
                return self._buf[self._start:end].copy()
            first = self._cap - self._start
            out = np.empty(self._len, dtype=np.float32)
            out[:first] = self._buf[self._start:]
            out[first:] = self._buf[:self._len - first]
            return out

    def reset(self) -> None:
        with self._lock:
            self._start = self._len = 0


class Recorder:
    """Owns one long-lived input stream.

    open()/close() bracket the app's life. begin()/end() bracket a dictation.
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        device=None,
        max_seconds: int = 1800,
        on_level=None,
        preroll_ms: int = DEFAULT_PREROLL_MS,
    ):
        # int() because the pre-roll capacity was built with an explicit cast
        # and the capture buffer's was not, so a float rate died inside
        # np.zeros with an opaque TypeError.
        sample_rate = int(sample_rate)
        self.sample_rate = sample_rate
        self.device = device
        self.on_level = on_level
        self.buffer = RingBuffer(sample_rate * max_seconds)
        self.preroll = RingBuffer(int(sample_rate * preroll_ms / 1000))
        self._stream = None
        self._capturing = False
        self._muted_until = 0.0

    # --- stream lifetime --------------------------------------------------

    def open(self) -> None:
        if self._stream is not None:
            return
        import sounddevice as sd

        self._stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=1,
            dtype="float32",
            device=self.device,
            blocksize=int(self.sample_rate / 60),
            callback=self._callback,
        )
        self._stream.start()
        log.info("microphone stream open (%dHz)", self.sample_rate)

    def close(self) -> None:
        self._capturing = False
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                log.exception("error closing the audio stream")

    # --- session ----------------------------------------------------------

    def begin(self) -> None:
        """Start capturing. Seeds with the pre-roll so the first word survives."""
        self.open()
        self.buffer.reset()
        pre = self.preroll.read_all()
        # Drain it. The pre-roll stops being fed for as long as a session is
        # capturing, so without this the SAME 400ms is prepended to the next
        # session too — and it can be arbitrarily old, since the end-of-session
        # cue mutes the pre-roll straight after. The opening words of one
        # dictation reappeared at the head of the next.
        self.preroll.reset()
        if pre.size:
            self.buffer.write(pre)
        self._capturing = True

    def mute_for(self, ms: int) -> None:
        """Keep an audio cue out of the PRE-ROLL for `ms`, starting now.

        This deliberately does NOT touch an active recording. It used to, and
        that was a serious bug: the mute ran for ~490ms from the moment the
        chord went down, which is exactly when people start talking. Every
        dictation lost its opening words — "Hey, can you add three items" came
        back as "You add three items" — and a short utterance vanished entirely,
        so nothing was sent at all.

        The trade was wrong in both directions. The cue is a quiet 90ms tone
        that real speech buries, and the one case it could corrupt — a
        recording of nothing but the cue — is already caught by the silence
        guard in app.py. Losing the user's words to protect against a
        hallucination on an empty clip is a bad bargain.

        The pre-roll is still muted, so the done and cancel cues of one session
        cannot leak into the next session's opening.
        """
        # max, not assignment. A 10ms cue following a 500ms one used to cut
        # the mute short by 490ms, so the tail of the first cue landed in the
        # next session's pre-roll -- exactly what the mute exists to prevent.
        self._muted_until = max(self._muted_until,
                                time.monotonic() + ms / 1000.0)

    def end(self) -> np.ndarray:
        """Stop capturing and return the audio. Does NOT close the device."""
        self._capturing = False
        return self.buffer.read_all()

    @property
    def capturing(self) -> bool:
        return self._capturing

    @property
    def running(self) -> bool:
        return self._stream is not None

    # --- callback thread --------------------------------------------------

    def _callback(self, indata, _frames, _time_info, status):
        if status:
            log.debug("audio status: %s", status)
        block = indata[:, 0] if indata.ndim > 1 else indata
        if self._capturing:
            # NEVER gated. An active recording is the user talking, and no cue
            # is worth dropping a syllable of it.
            self.buffer.write(block)
            if self.on_level is not None:
                try:
                    self.on_level(rms(block))
                except Exception:
                    log.exception("on_level raised; continuing capture")
        elif time.monotonic() >= self._muted_until:
            # Idle: keep only the rolling pre-roll window. Nothing is stored,
            # nothing leaves this buffer, and it is overwritten continuously.
            # Muted while a cue sounds, so one session's done tone cannot end up
            # at the front of the next session's audio.
            self.preroll.write(block)
