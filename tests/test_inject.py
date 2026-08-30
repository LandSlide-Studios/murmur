import pytest

from murmur.inject import Injector


def _wire(inj, monkeypatch, clip, order=None):
    monkeypatch.setattr(inj, "_get_clipboard", lambda: clip["v"])
    monkeypatch.setattr(inj, "_set_clipboard", lambda t: clip.__setitem__("v", t))
    def release():
        if order is not None:
            order.append("release")
        return True                 # modifiers cleared

    monkeypatch.setattr(inj, "_release_modifiers", release)
    monkeypatch.setattr(
        inj, "_send_paste",
        lambda: order.append("paste") if order is not None else None)


def test_restore_flag_false_leaves_text_on_clipboard(monkeypatch):
    clip = {"v": "OLD"}
    inj = Injector(restore_previous=False, clipboard_settle_s=0)
    _wire(inj, monkeypatch, clip)
    inj.inject("NEW")
    assert clip["v"] == "NEW"


def test_restore_flag_true_restores_previous_clipboard(monkeypatch):
    clip = {"v": "OLD"}
    inj = Injector(restore_previous=True, clipboard_settle_s=0, restore_delay_s=0)
    _wire(inj, monkeypatch, clip)
    inj.inject("NEW")
    assert clip["v"] == "OLD"


def test_modifiers_are_released_before_paste(monkeypatch):
    """Ctrl+V sent while Ctrl+Win are still down reads as Ctrl+Win+V and opens
    Clipboard History instead of pasting."""
    order, clip = [], {"v": ""}
    inj = Injector(restore_previous=False, clipboard_settle_s=0)
    _wire(inj, monkeypatch, clip, order)
    inj.inject("x")
    assert order == ["release", "paste"]


def test_empty_text_is_a_no_op(monkeypatch):
    order, clip = [], {"v": "OLD"}
    inj = Injector(restore_previous=False, clipboard_settle_s=0)
    _wire(inj, monkeypatch, clip, order)
    inj.inject("")
    assert order == [] and clip["v"] == "OLD"


def test_none_text_is_a_no_op(monkeypatch):
    order, clip = [], {"v": "OLD"}
    inj = Injector(restore_previous=False, clipboard_settle_s=0)
    _wire(inj, monkeypatch, clip, order)
    inj.inject(None)
    assert order == [] and clip["v"] == "OLD"


def test_whitespace_only_text_still_pastes(monkeypatch):
    """A deliberate newline is legitimate output; only empty is a no-op."""
    order, clip = [], {"v": "OLD"}
    inj = Injector(restore_previous=False, clipboard_settle_s=0)
    _wire(inj, monkeypatch, clip, order)
    inj.inject("\n")
    assert order == ["release", "paste"]


def test_clipboard_read_failure_does_not_prevent_the_paste(monkeypatch):
    """Losing the previous clipboard is acceptable; losing the dictation is not."""
    order = []
    inj = Injector(restore_previous=True, clipboard_settle_s=0, restore_delay_s=0)
    monkeypatch.setattr(inj, "_get_clipboard", lambda: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(inj, "_set_clipboard", lambda t: None)
    monkeypatch.setattr(inj, "_release_modifiers",
                        lambda: (order.append("release"), True)[1])
    monkeypatch.setattr(inj, "_send_paste", lambda: order.append("paste"))
    inj.inject("text")
    assert "paste" in order


def test_restore_failure_does_not_raise(monkeypatch):
    calls = {"n": 0}

    def flaky_set(t):
        calls["n"] += 1
        if calls["n"] > 1:
            raise OSError("clipboard locked by another process")

    inj = Injector(restore_previous=True, clipboard_settle_s=0, restore_delay_s=0)
    monkeypatch.setattr(inj, "_get_clipboard", lambda: "OLD")
    monkeypatch.setattr(inj, "_set_clipboard", flaky_set)
    monkeypatch.setattr(inj, "_release_modifiers", lambda: True)
    monkeypatch.setattr(inj, "_send_paste", lambda: None)
    inj.inject("NEW")          # must not raise


def test_set_clipboard_failure_is_raised_so_the_caller_can_react(monkeypatch):
    """If the text never reached the clipboard, Ctrl+V would paste something
    else entirely — that is worse than failing loudly."""
    inj = Injector(restore_previous=False, clipboard_settle_s=0)
    monkeypatch.setattr(inj, "_get_clipboard", lambda: "OLD")
    monkeypatch.setattr(
        inj, "_set_clipboard", lambda t: (_ for _ in ()).throw(OSError("locked")))
    monkeypatch.setattr(inj, "_release_modifiers", lambda: True)
    monkeypatch.setattr(
        inj, "_send_paste", lambda: pytest.fail("must not paste a stale clipboard"))
    with pytest.raises(OSError):
        inj.inject("NEW")


# --- split copy/paste, so the comet can fly between them ---

def test_copy_puts_text_on_the_clipboard_without_pasting(monkeypatch):
    order, clip = [], {"v": "OLD"}
    inj = Injector(restore_previous=False, clipboard_settle_s=0)
    _wire(inj, monkeypatch, clip, order)
    assert inj.copy("NEW") is True
    assert clip["v"] == "NEW"
    assert "paste" not in order, "copy() must not send the keystroke"


def test_copy_returns_false_when_a_modifier_is_stuck(monkeypatch):
    clip = {"v": ""}
    inj = Injector(restore_previous=False, clipboard_settle_s=0)
    monkeypatch.setattr(inj, "_get_clipboard", lambda: clip["v"])
    monkeypatch.setattr(inj, "_set_clipboard", lambda t: clip.__setitem__("v", t))
    monkeypatch.setattr(inj, "_release_modifiers", lambda: False)
    assert inj.copy("NEW") is False
    assert clip["v"] == "NEW", "the text must reach the clipboard either way"


def test_paste_sends_the_keystroke(monkeypatch):
    order = []
    inj = Injector(restore_previous=False)
    monkeypatch.setattr(inj, "_send_paste", lambda: order.append("paste"))
    inj.paste()
    assert order == ["paste"]


def test_copy_of_empty_text_is_a_no_op(monkeypatch):
    inj = Injector(restore_previous=False)
    monkeypatch.setattr(inj, "_release_modifiers",
                        lambda: (_ for _ in ()).throw(AssertionError("touched")))
    assert inj.copy("") is False
