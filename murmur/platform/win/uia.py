"""Reading text back out of the app we pasted into, via UI Automation.

This is capture path B for vocabulary learning. It works for Win32 edits,
RichEdit, UWP, and Chromium/Electron apps with accessibility enabled (VS Code,
Slack, Discord, browsers). Canvas-rendered editors expose nothing at all.

Every failure here is expected and non-fatal: the caller degrades to the
clipboard path. Nothing in this module may raise into the app.
"""

import logging

log = logging.getLogger(__name__)


class UIAReader:
    def __init__(self):
        self._uia = None
        self._mod = None
        try:
            import comtypes.client

            self._mod = comtypes.client.GetModule("UIAutomationCore.dll")
            self._uia = comtypes.client.CreateObject(
                "{ff48dba4-60ef-4201-aa87-54103eef594e}",
                interface=self._mod.IUIAutomation,
            )
        except Exception as e:
            log.info("UI Automation unavailable; using the clipboard path: %s", e)

    @property
    def available(self) -> bool:
        return self._uia is not None

    def snapshot(self):
        """Capture a handle to the focused text control, plus its text now."""
        if not self.available:
            return None
        try:
            element = self._uia.GetFocusedElement()
            if not element:
                return None
            return {"element": element, "text": self._text_of(element)}
        except Exception:
            log.debug("UIA snapshot failed", exc_info=True)
            return None

    def read(self, snap):
        """Re-read the same control. None when it is gone or exposes no text."""
        if not self.available or not snap:
            return None
        try:
            return self._text_of(snap["element"])
        except Exception:
            log.debug("UIA read failed", exc_info=True)
            return None

    def _text_of(self, element):
        mod = self._mod
        try:
            pattern = element.GetCurrentPattern(mod.UIA_TextPatternId)
            if pattern:
                text_pattern = pattern.QueryInterface(mod.IUIAutomationTextPattern)
                return text_pattern.DocumentRange.GetText(-1)
        except Exception:
            pass
        try:
            pattern = element.GetCurrentPattern(mod.UIA_ValuePatternId)
            if pattern:
                value_pattern = pattern.QueryInterface(mod.IUIAutomationValuePattern)
                return value_pattern.CurrentValue
        except Exception:
            pass
        return None
