#!/usr/bin/env python3
"""
TheSeer Notification Server
===========================
Menu-bar app (👁) that:
  • reads JSON notification requests from a FIFO pipe
  • draws custom toaster panels in the upper-right corner
  • supports hover (pointer cursor), expand ("more ▾"), and click-to-chat
  • keeps a history of recent notifications, accessible from the tray menu
  • opens a centered chat window when a toaster body is clicked
  • pauses screen inference while a chat is open

FIFO contract (one JSON object per line):
  {"title": "...", "subtitle": "...", "body": "...", "persona": "..."}

Coordination with theSeer.py:
  When a chat opens we touch  /tmp/theseer_chat_active
  theSeer.py checks for that file each tick and skips inference if present.
"""
import fcntl
import json
import os
import queue
import random
import sys
import threading
import time
from collections import deque
from datetime import datetime

import requests
import objc
import rumps
import AppKit
import Foundation  # noqa: F401 — needed for PyObjC's Foundation init

import configuration as cfg
import personas      as p

# pynput is optional — if it's not installed, global hotkeys are silently
# disabled (everything else still works). If it IS installed but macOS
# Accessibility permission hasn't been granted, the listener starts but
# never fires events — no error is raised.
try:
    from pynput import keyboard as _kb
    _HOTKEYS_AVAILABLE = True
except ImportError:
    _kb = None
    _HOTKEYS_AVAILABLE = False

# ── Tunables ───────────────────────────────────────────────────────
FIFO              = "/tmp/theseer_notify"
PAUSE_FILE        = "/tmp/theseer_chat_active"
LOCK_FILE         = "/tmp/theseer_notify_server.pid"  # singleton lock
OVERRIDE_FILE     = "/tmp/theseer_next_persona"       # hotkey → theSeer next-tick override
PERF_TOGGLE_FILE  = "/tmp/theseer_perf_toggle"        # Cmd+2 → flip performance mode

# Personas that Cmd+` is allowed to pick at random. Auditor is excluded
# (security shouldn't fire spuriously); performance is excluded because
# it's a sticky mode entered via phrases, not a one-tick override.
RANDOM_PERSONA_POOL = (
    "casual", "engineer", "executive",
    "entertainer", "teacher", "motivator", "sassy",
)

PANEL_WIDTH       = 360
PANEL_HEIGHT      = 100
PANEL_MARGIN      = 14
PANEL_STACK_GAP   = 8
DISMISS_AFTER_SEC = 9.0     # how long a toaster stays on screen before auto-dismissing
TICK_SEC          = 0.1

# Body label vertical area
TITLE_H           = 18
SUBTITLE_H        = 16
HINT_H            = 14
BODY_TOP_PAD      = 4
BODY_BOTTOM_PAD   = 18   # leaves room for hint + button
HORIZONTAL_PAD    = 14

SOUND_NAME        = "Frog"   # macOS system sound; set to None to mute entirely
SOUND_VOLUME      = 0.3
SOUND_MIN_GAP_SEC = 1.0      # Don't play another sound within this window of the
                             # previous one. Stops overlapping plays from buzzing
                             # when several toasters arrive in quick succession
                             # (e.g. from Cmd+0 replay).

CHAT_W            = 540
CHAT_H            = 620
CHAT_MAX_TOKENS   = 300

HISTORY_LIMIT     = 20
# ───────────────────────────────────────────────────────────────────


# ──────────────────────────────────────────────────────────────────
# Custom NSVisualEffectView with click + hover handling
# ──────────────────────────────────────────────────────────────────

class ToasterContentView(AppKit.NSVisualEffectView):
    """An NSVisualEffectView that:
      • forwards mouseDown_ to a Python callable (the click-to-chat handler)
      • swaps cursor + applies a subtle tint on hover
    """

    @objc.python_method
    def install(self, on_click, on_hover_change):
        """Wire up the handlers after construction. Python-only setter."""
        self._on_click        = on_click
        self._on_hover_change = on_hover_change
        self._tracking_area   = None
        self._refresh_tracking()

    @objc.python_method
    def _refresh_tracking(self):
        if self._tracking_area is not None:
            self.removeTrackingArea_(self._tracking_area)
        opts = (
            AppKit.NSTrackingMouseEnteredAndExited
            | AppKit.NSTrackingActiveAlways
            | AppKit.NSTrackingInVisibleRect
        )
        self._tracking_area = (
            AppKit.NSTrackingArea.alloc()
            .initWithRect_options_owner_userInfo_(self.bounds(), opts, self, None)
        )
        self.addTrackingArea_(self._tracking_area)

    # ObjC overrides — these MUST be present for the runtime to dispatch to us

    def updateTrackingAreas(self):
        self._refresh_tracking()
        objc.super(ToasterContentView, self).updateTrackingAreas()

    def mouseDown_(self, event):
        cb = getattr(self, "_on_click", None)
        if cb is not None:
            cb()

    def mouseEntered_(self, event):
        AppKit.NSCursor.pointingHandCursor().push()
        cb = getattr(self, "_on_hover_change", None)
        if cb is not None:
            cb(True)

    def mouseExited_(self, event):
        try:
            AppKit.NSCursor.pop()
        except Exception:
            pass
        cb = getattr(self, "_on_hover_change", None)
        if cb is not None:
            cb(False)


# ──────────────────────────────────────────────────────────────────
# Chat window (unchanged from previous version — centered conversation)
# ──────────────────────────────────────────────────────────────────

class ChatWindow(AppKit.NSObject):
    """Centered NSWindow that drives one LLM conversation for one toaster."""

    def initWithData_onClose_(self, data, on_close):
        self = objc.super(ChatWindow, self).init()
        if self is None:
            return None
        self._data     = data
        self._on_close = on_close
        self._messages = self._initial_messages()
        self._busy     = False
        self._build_window()
        self._render()
        return self

    @objc.python_method
    def _initial_messages(self):
        persona_name = self._data.get("persona", "casual")
        persona_data = p.PERSONAS.get(persona_name, {})
        base_prompt  = persona_data.get(
            "prompt",
            "You are TheSeer, a helpful ambient assistant.",
        )
        sys_prompt = (
            f"{base_prompt} "
            "You're now chatting with the user about a notification you just "
            "sent. Help them understand or act on it. Be concise — short "
            "paragraphs, no walls of text. Stay in your persona."
        )
        return [
            {"role": "system",    "content": sys_prompt},
            {"role": "assistant", "content": self._data.get("body", "")},
        ]

    @objc.python_method
    def _build_window(self):
        screen = AppKit.NSScreen.mainScreen().visibleFrame()
        x = screen.origin.x + (screen.size.width  - CHAT_W) / 2.0
        y = screen.origin.y + (screen.size.height - CHAT_H) / 2.0

        self.window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            ((x, y), (CHAT_W, CHAT_H)),
            AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskClosable,
            AppKit.NSBackingStoreBuffered,
            False,
        )
        self.window.setTitle_(f"💬 TheSeer  ·  {self._data.get('title', 'Chat')}")
        self.window.setDelegate_(self)
        self.window.setReleasedWhenClosed_(False)
        self.window.setLevel_(AppKit.NSFloatingWindowLevel)

        content = AppKit.NSView.alloc().initWithFrame_(((0, 0), (CHAT_W, CHAT_H)))

        scroll_h = CHAT_H - 100
        scroll = AppKit.NSScrollView.alloc().initWithFrame_(((12, 60), (CHAT_W - 24, scroll_h)))
        scroll.setHasVerticalScroller_(True)
        scroll.setAutohidesScrollers_(True)
        scroll.setBorderType_(AppKit.NSNoBorder)
        scroll.setDrawsBackground_(False)

        text_view = AppKit.NSTextView.alloc().initWithFrame_(((0, 0), (CHAT_W - 24, scroll_h)))
        text_view.setEditable_(False)
        text_view.setSelectable_(True)
        text_view.setRichText_(True)
        text_view.setDrawsBackground_(False)
        text_view.setTextContainerInset_((6, 6))
        scroll.setDocumentView_(text_view)
        self.text_view = text_view

        self.input_field = AppKit.NSTextField.alloc().initWithFrame_(((12, 16), (CHAT_W - 100, 28)))
        self.input_field.setPlaceholderString_("Ask TheSeer about this…")
        self.input_field.setBezeled_(True)
        self.input_field.setBezelStyle_(AppKit.NSTextFieldRoundedBezel)
        self.input_field.setFont_(AppKit.NSFont.systemFontOfSize_(13))
        self.input_field.setDelegate_(self)

        send_btn = AppKit.NSButton.alloc().initWithFrame_(((CHAT_W - 82, 14), (70, 32)))
        send_btn.setTitle_("Send")
        send_btn.setBezelStyle_(AppKit.NSBezelStyleRounded)
        send_btn.setTarget_(self)
        send_btn.setAction_("sendMessage:")
        send_btn.setKeyEquivalent_("\r")
        self.send_btn = send_btn

        content.addSubview_(scroll)
        content.addSubview_(self.input_field)
        content.addSubview_(send_btn)

        self.window.setContentView_(content)

        # IMPORTANT: rumps runs us as an accessory app (no Dock icon), which
        # means macOS won't route keystrokes to our windows even if they're
        # visible. Temporarily promote to "regular" so the input field can
        # actually receive typing. We demote back in windowWillClose_.
        AppKit.NSApp.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)
        AppKit.NSApp.activateIgnoringOtherApps_(True)
        self.window.makeKeyAndOrderFront_(None)
        self.window.makeFirstResponder_(self.input_field)

    @objc.python_method
    def _render(self):
        body_font  = AppKit.NSFont.systemFontOfSize_(13)
        label_font = AppKit.NSFont.boldSystemFontOfSize_(13)
        full       = AppKit.NSMutableAttributedString.alloc().init()

        for msg in self._messages:
            if msg["role"] == "system":
                continue
            if msg["role"] == "user":
                label, color = "You: ", AppKit.NSColor.systemBlueColor()
            else:
                label, color = "TheSeer: ", AppKit.NSColor.systemPurpleColor()

            full.appendAttributedString_(
                AppKit.NSAttributedString.alloc().initWithString_attributes_(
                    label,
                    {AppKit.NSFontAttributeName: label_font,
                     AppKit.NSForegroundColorAttributeName: color},
                ))
            full.appendAttributedString_(
                AppKit.NSAttributedString.alloc().initWithString_attributes_(
                    msg["content"] + "\n\n",
                    {AppKit.NSFontAttributeName: body_font,
                     AppKit.NSForegroundColorAttributeName: AppKit.NSColor.labelColor()},
                ))

        self.text_view.textStorage().setAttributedString_(full)
        self.text_view.scrollRangeToVisible_((full.length(), 0))

    def sendMessage_(self, sender):
        if self._busy:
            return
        text = str(self.input_field.stringValue()).strip()
        if not text:
            return

        self.input_field.setStringValue_("")
        self._messages.append({"role": "user",      "content": text})
        self._messages.append({"role": "assistant", "content": "…"})
        self._render()

        self._busy = True
        self.input_field.setEnabled_(False)
        self.send_btn.setEnabled_(False)

        threading.Thread(target=self._llm_call, daemon=True).start()

    @objc.python_method
    def _llm_call(self):
        history = [m for m in self._messages if m["content"] != "…"]
        try:
            response = requests.post(
                cfg.MLX_SERVER_URL,
                json={"model": cfg.MODEL_ID, "messages": history, "max_tokens": CHAT_MAX_TOKENS},
                timeout=60,
            )
            if response.status_code == 200:
                reply = response.json()["choices"][0]["message"]["content"].strip()
            else:
                reply = f"⚠️ MLX returned status {response.status_code}"
        except Exception as e:
            reply = f"⚠️ Error contacting MLX: {e}"

        AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(
            lambda: self._on_reply(reply)
        )

    @objc.python_method
    def _on_reply(self, reply):
        if self._messages and self._messages[-1]["content"] == "…":
            self._messages[-1]["content"] = reply
        else:
            self._messages.append({"role": "assistant", "content": reply})
        self._render()
        self._busy = False
        self.input_field.setEnabled_(True)
        self.send_btn.setEnabled_(True)
        self.window.makeFirstResponder_(self.input_field)

    def control_textView_doCommandBySelector_(self, control, textView, selector):
        if str(selector) == "insertNewline:":
            self.sendMessage_(None)
            return True
        return False

    def windowWillClose_(self, notification):
        # Demote back to accessory so we go back to being menu-bar-only
        # (no Dock icon, no app switcher entry).
        AppKit.NSApp.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)

        if self._on_close:
            try:
                self._on_close()
            except Exception as e:
                print(f"[chat] on_close error: {e}")


# ──────────────────────────────────────────────────────────────────
# Notification server
# ──────────────────────────────────────────────────────────────────

class TheSeerNotifyServer(rumps.App):

    def __init__(self):
        super().__init__("👁", quit_button=None)
        # Menu structure: status / History submenu / Quit
        # We build the History submenu dynamically.
        self.menu = [
            "TheSeer is watching...",
            None,
            ("History", ["(no notifications yet)"]),
            None,
            "Quit",
        ]

        self._pending          = queue.Queue()
        self._panels           = []      # active toasters: list of dicts
        self._chat             = None    # active ChatWindow
        self._history          = deque(maxlen=HISTORY_LIMIT)
        self._last_sound_at    = 0.0     # rate-limit timer for notification sound
        self._active_sounds    = []      # strong refs so NSSound can't be GC'd mid-play

        if not os.path.exists(FIFO):
            os.mkfifo(FIFO)
        if os.path.exists(PAUSE_FILE):
            try: os.remove(PAUSE_FILE)
            except: pass

        threading.Thread(target=self._watch_fifo, daemon=True).start()
        self._tick_timer = rumps.Timer(self._tick, TICK_SEC)
        self._tick_timer.start()

        # ── Global hotkeys (Cmd+0, Cmd+9) ────────────────────────────
        # pynput runs the listener on its own thread; we bounce events
        # onto the main NSOperationQueue before touching any UI.
        self._hotkey_listener = None
        if _HOTKEYS_AVAILABLE:
            try:
                self._hotkey_listener = _kb.GlobalHotKeys({
                    "<cmd>+0": self._hotkey_replay_all,
                    "<cmd>+9": self._hotkey_open_last_chat,
                    "<cmd>+1": self._hotkey_sass,
                    "<cmd>+2": self._hotkey_toggle_performance,
                    "<cmd>+`": self._hotkey_random_persona,
                })
                self._hotkey_listener.start()
                print("[notify_server] hotkeys active: "
                      "Cmd+0 replay-all · Cmd+9 toggle-chat · "
                      "Cmd+1 sassy next tick · Cmd+2 toggle performance mode · "
                      "Cmd+` random persona next tick. "
                      "If they don't fire, grant Accessibility permission to "
                      "python3 in System Settings.")
            except Exception as e:
                print(f"[notify_server] hotkey listener failed to start: {e}")
        else:
            print("[notify_server] pynput not installed — hotkeys disabled. "
                  "Run: pip install pynput")

    # ── FIFO reader thread ──────────────────────────────────────────

    def _watch_fifo(self):
        while True:
            try:
                with open(FIFO, "r") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            self._pending.put(json.loads(line))
                        except Exception as e:
                            print(f"[notify_server] parse error: {e}")
            except Exception as e:
                print(f"[notify_server] fifo error: {e}")

    # ── Main-thread tick ────────────────────────────────────────────

    def _tick(self, _timer):
        # 1. Drain pending notifications
        while True:
            try:
                msg = self._pending.get_nowait()
            except queue.Empty:
                break
            is_meta = bool(msg.get("meta"))   # startup / system pings
            if self._chat is not None:
                # Chat open: keep real notifications in history so the user can
                # replay them later; meta ones are skipped entirely.
                if not is_meta:
                    self._append_history(msg)
                continue
            self._show_toaster(
                msg.get("title",    "TheSeer"),
                msg.get("subtitle", "👁 TheSeer"),
                msg.get("body",     ""),
                msg.get("persona",  "casual"),
                record=not is_meta,   # meta pings show but don't enter history
            )

        # 2. Expire old panels (skip expanded ones — they stay until clicked)
        now = time.monotonic()
        kept, removed = [], False
        for entry in self._panels:
            if entry["is_expanded"]:
                kept.append(entry)
            elif entry["dismiss_at"] <= now:
                try: entry["panel"].orderOut_(None)
                except: pass
                removed = True
            else:
                kept.append(entry)
        if removed:
            self._panels = kept
            self._restack_panels()

    # ── Toaster construction ────────────────────────────────────────

    def _show_toaster(self, title, subtitle, body, persona, record=True):
        """Draw a toaster panel. When `record` is False, skip adding to
        history (used by hotkey replays of already-historical entries)."""
        screen = AppKit.NSScreen.mainScreen()
        if screen is None:
            return
        visible = screen.visibleFrame()

        # Measure body text height so we know if we need a "more ▾" button
        body_w        = PANEL_WIDTH - 2 * HORIZONTAL_PAD
        avail_body_h  = PANEL_HEIGHT - TITLE_H - SUBTITLE_H - BODY_BOTTOM_PAD
        body_full_h   = self._measure_body_height(body, body_w)
        needs_more    = body_full_h > avail_body_h + 2   # +2px tolerance

        x = visible.origin.x + visible.size.width - PANEL_WIDTH - PANEL_MARGIN
        y_top = visible.origin.y + visible.size.height - PANEL_MARGIN \
              - sum(e["panel"].frame().size.height + PANEL_STACK_GAP for e in self._panels)
        y = y_top - PANEL_HEIGHT

        style_mask = (
            AppKit.NSWindowStyleMaskBorderless
            | AppKit.NSWindowStyleMaskNonactivatingPanel
        )
        panel = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            ((x, y), (PANEL_WIDTH, PANEL_HEIGHT)),
            style_mask,
            AppKit.NSBackingStoreBuffered,
            False,
        )
        panel.setLevel_(AppKit.NSStatusWindowLevel)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(AppKit.NSColor.clearColor())
        panel.setHasShadow_(True)
        panel.setMovableByWindowBackground_(False)
        panel.setHidesOnDeactivate_(False)
        panel.setBecomesKeyOnlyIfNeeded_(True)
        panel.setCollectionBehavior_(
            AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
            | AppKit.NSWindowCollectionBehaviorStationary
            | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
        )

        # Custom clickable content view
        content = ToasterContentView.alloc().initWithFrame_(((0, 0), (PANEL_WIDTH, PANEL_HEIGHT)))
        content.setMaterial_(AppKit.NSVisualEffectMaterialPopover)
        content.setBlendingMode_(AppKit.NSVisualEffectBlendingModeBehindWindow)
        content.setState_(AppKit.NSVisualEffectStateActive)
        content.setWantsLayer_(True)
        content.layer().setCornerRadius_(12.0)
        content.layer().setMasksToBounds_(True)

        # --- Title (top) ---
        title_lbl = self._label(
            ((HORIZONTAL_PAD, PANEL_HEIGHT - 6 - TITLE_H),
             (PANEL_WIDTH - 2*HORIZONTAL_PAD, TITLE_H)),
            title, AppKit.NSFont.boldSystemFontOfSize_(13),
            AppKit.NSColor.labelColor(),
        )
        content.addSubview_(title_lbl)

        # --- Subtitle ---
        subtitle_lbl = self._label(
            ((HORIZONTAL_PAD, PANEL_HEIGHT - 6 - TITLE_H - SUBTITLE_H),
             (PANEL_WIDTH - 2*HORIZONTAL_PAD, SUBTITLE_H)),
            subtitle, AppKit.NSFont.systemFontOfSize_(11),
            AppKit.NSColor.secondaryLabelColor(),
        )
        content.addSubview_(subtitle_lbl)

        # --- Body (multi-line, wraps) ---
        body_y      = BODY_BOTTOM_PAD
        body_label  = self._label(
            ((HORIZONTAL_PAD, body_y),
             (body_w, PANEL_HEIGHT - TITLE_H - SUBTITLE_H - BODY_BOTTOM_PAD - 6)),
            body, AppKit.NSFont.systemFontOfSize_(12),
            AppKit.NSColor.labelColor(), wraps=True,
        )
        content.addSubview_(body_label)

        # --- "click to chat ↗" hint (always visible, low key) ---
        hint = self._label(
            ((HORIZONTAL_PAD, 2),
             (PANEL_WIDTH - 2*HORIZONTAL_PAD - 60, HINT_H)),
            "click to chat ↗",
            AppKit.NSFont.systemFontOfSize_(10),
            AppKit.NSColor.tertiaryLabelColor(),
        )
        content.addSubview_(hint)

        # --- Expand button "more ▾" (only if body overflows) ---
        expand_btn = None
        if needs_more:
            btn_w, btn_h = 56, 18
            expand_btn = AppKit.NSButton.alloc().initWithFrame_(
                ((PANEL_WIDTH - HORIZONTAL_PAD - btn_w, 1), (btn_w, btn_h))
            )
            expand_btn.setBezelStyle_(AppKit.NSBezelStyleInline)
            expand_btn.setTitle_("more ▾")
            expand_btn.setFont_(AppKit.NSFont.systemFontOfSize_(10))
            expand_btn.setTarget_(self)
            expand_btn.setAction_("toggleExpand:")
            content.addSubview_(expand_btn)

        # Build the entry record BEFORE wiring click handlers (we close over it)
        entry = {
            "panel":          panel,
            "content":        content,
            "title_lbl":      title_lbl,
            "subtitle_lbl":   subtitle_lbl,
            "body_label":     body_label,
            "hint":           hint,
            "expand_btn":     expand_btn,
            "dismiss_at":     time.monotonic() + DISMISS_AFTER_SEC,
            "is_expanded":    False,
            "body_full_text": body,
            "body_full_h":    body_full_h,
            "data": {
                "title":    title,
                "subtitle": subtitle,
                "body":     body,
                "persona":  persona,
            },
        }

        # Wire click + hover into the custom view
        def _on_click(_entry=entry):
            self._toaster_clicked(_entry)
        def _on_hover_change(is_in, _entry=entry):
            self._toaster_hover(_entry, is_in)
        content.install(_on_click, _on_hover_change)

        panel.setContentView_(content)
        panel.orderFrontRegardless()

        self._panels.append(entry)
        if record:
            self._append_history({
                "title":    title,
                "subtitle": subtitle,
                "body":     body,
                "persona":  persona,
            })

        # Subtle sound — only for fresh notifications (not replays), and
        # rate-limited so back-to-back toasters don't overlap-buzz.
        if SOUND_NAME and record:
            now = time.monotonic()
            if (now - self._last_sound_at) >= SOUND_MIN_GAP_SEC:
                self._last_sound_at = now
                try:
                    base = AppKit.NSSound.soundNamed_(SOUND_NAME)
                    if base:
                        snd = base.copy()
                        snd.setVolume_(SOUND_VOLUME)
                        # Hold a strong ref so PyObjC can't release the
                        # NSSound before playback finishes (a common cause
                        # of audio glitches with autoreleased copies).
                        self._active_sounds.append(snd)
                        snd.play()
                        # Prune any finished sounds so the list doesn't grow
                        self._active_sounds = [
                            s for s in self._active_sounds if s.isPlaying()
                        ]
                except Exception as e:
                    print(f"[notify_server] sound error: {e}")

    def _label(self, frame, text, font, color, wraps=False):
        lbl = AppKit.NSTextField.alloc().initWithFrame_(frame)
        lbl.setStringValue_(text or "")
        lbl.setEditable_(False)
        lbl.setSelectable_(False)
        lbl.setBezeled_(False)
        lbl.setDrawsBackground_(False)
        lbl.setFont_(font)
        lbl.setTextColor_(color)
        if wraps:
            lbl.cell().setWraps_(True)
            lbl.cell().setLineBreakMode_(AppKit.NSLineBreakByWordWrapping)
        return lbl

    @objc.python_method
    def _measure_body_height(self, text, width):
        """How tall does this body text need to be at the given width?"""
        if not text:
            return 0
        attrs = {AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_(12)}
        ns = AppKit.NSString.stringWithString_(text)
        rect = ns.boundingRectWithSize_options_attributes_(
            (width, 9999.0),
            AppKit.NSStringDrawingUsesLineFragmentOrigin,
            attrs,
        )
        return int(rect.size.height) + 2

    def _restack_panels(self):
        screen = AppKit.NSScreen.mainScreen()
        if screen is None:
            return
        visible = screen.visibleFrame()
        y_top = visible.origin.y + visible.size.height - PANEL_MARGIN
        x = visible.origin.x + visible.size.width - PANEL_WIDTH - PANEL_MARGIN
        for entry in self._panels:
            h = entry["panel"].frame().size.height
            entry["panel"].setFrameOrigin_((x, y_top - h))
            y_top -= h + PANEL_STACK_GAP

    # ── Hover ───────────────────────────────────────────────────────

    @objc.python_method
    def _toaster_hover(self, entry, is_in):
        # Subtle: brighten the hint text on hover, dim it otherwise
        hint = entry.get("hint")
        if hint is None:
            return
        hint.setTextColor_(
            AppKit.NSColor.secondaryLabelColor() if is_in
            else AppKit.NSColor.tertiaryLabelColor()
        )

    # ── Click on the body → open chat ───────────────────────────────

    @objc.python_method
    def _toaster_clicked(self, entry):
        # Dismiss the toaster
        try: entry["panel"].orderOut_(None)
        except: pass
        if entry in self._panels:
            self._panels.remove(entry)
            self._restack_panels()
        try:
            AppKit.NSCursor.pop()
        except Exception:
            pass

        # Focus existing chat if open
        if self._chat is not None:
            self._chat.window.makeKeyAndOrderFront_(None)
            AppKit.NSApp.activateIgnoringOtherApps_(True)
            return

        self._open_chat(entry["data"])

    # ── Click on the "more ▾" / "less ▴" button → expand/collapse ───

    def toggleExpand_(self, sender):
        # Find the entry that owns this button
        entry = next((e for e in self._panels if e["expand_btn"] is sender), None)
        if entry is None:
            return

        if entry["is_expanded"]:
            self._collapse_panel(entry)
        else:
            self._expand_panel(entry)

    @objc.python_method
    def _expand_panel(self, entry):
        body_w     = PANEL_WIDTH - 2 * HORIZONTAL_PAD
        body_h     = max(entry["body_full_h"], 40)
        new_h      = TITLE_H + SUBTITLE_H + body_h + BODY_BOTTOM_PAD + 8

        frame = entry["panel"].frame()
        top   = frame.origin.y + frame.size.height
        entry["panel"].setFrame_display_animate_(
            ((frame.origin.x, top - new_h), (frame.size.width, new_h)),
            True, False,
        )

        # Reflow subviews inside the new height
        entry["content"].setFrame_(((0, 0), (PANEL_WIDTH, new_h)))
        entry["title_lbl"].setFrame_(
            ((HORIZONTAL_PAD, new_h - 6 - TITLE_H),
             (body_w, TITLE_H))
        )
        entry["subtitle_lbl"].setFrame_(
            ((HORIZONTAL_PAD, new_h - 6 - TITLE_H - SUBTITLE_H),
             (body_w, SUBTITLE_H))
        )
        entry["body_label"].setFrame_(
            ((HORIZONTAL_PAD, BODY_BOTTOM_PAD), (body_w, body_h))
        )
        entry["expand_btn"].setTitle_("less ▴")

        entry["is_expanded"] = True
        self._restack_panels()

    @objc.python_method
    def _collapse_panel(self, entry):
        frame = entry["panel"].frame()
        top   = frame.origin.y + frame.size.height
        entry["panel"].setFrame_display_animate_(
            ((frame.origin.x, top - PANEL_HEIGHT), (frame.size.width, PANEL_HEIGHT)),
            True, False,
        )

        body_w = PANEL_WIDTH - 2 * HORIZONTAL_PAD
        entry["content"].setFrame_(((0, 0), (PANEL_WIDTH, PANEL_HEIGHT)))
        entry["title_lbl"].setFrame_(
            ((HORIZONTAL_PAD, PANEL_HEIGHT - 6 - TITLE_H),
             (body_w, TITLE_H))
        )
        entry["subtitle_lbl"].setFrame_(
            ((HORIZONTAL_PAD, PANEL_HEIGHT - 6 - TITLE_H - SUBTITLE_H),
             (body_w, SUBTITLE_H))
        )
        entry["body_label"].setFrame_(
            ((HORIZONTAL_PAD, BODY_BOTTOM_PAD),
             (body_w, PANEL_HEIGHT - TITLE_H - SUBTITLE_H - BODY_BOTTOM_PAD - 6))
        )
        entry["expand_btn"].setTitle_("more ▾")

        entry["is_expanded"] = False
        # Re-arm dismiss timer so it doesn't vanish instantly after collapse
        entry["dismiss_at"]  = time.monotonic() + DISMISS_AFTER_SEC
        self._restack_panels()

    # ── History ─────────────────────────────────────────────────────

    @objc.python_method
    def _append_history(self, data):
        self._history.append({
            "displayed_at": datetime.now(),
            "data":         dict(data),
        })
        self._rebuild_history_menu()

    @objc.python_method
    def _rebuild_history_menu(self):
        """Replace the History submenu with fresh items."""
        history_menu = self.menu.get("History")
        if history_menu is None:
            return
        # Clear current children
        for key in list(history_menu.keys()):
            del history_menu[key]

        if not self._history:
            history_menu.add(rumps.MenuItem("(no notifications yet)"))
            return

        # Most recent first
        for entry in reversed(self._history):
            ts = entry["displayed_at"].strftime("%H:%M")
            persona = entry["data"].get("persona", "?")
            body_preview = (entry["data"].get("body", "") or "")[:50].replace("\n", " ")
            label = f"{ts} · {persona}: {body_preview}"
            item = rumps.MenuItem(label, callback=self._make_history_handler(entry["data"]))
            history_menu.add(item)

        history_menu.add(None)
        history_menu.add(rumps.MenuItem("Clear history", callback=self._clear_history))

    @objc.python_method
    def _make_history_handler(self, data):
        def handler(_sender):
            if self._chat is not None:
                self._chat.window.makeKeyAndOrderFront_(None)
                AppKit.NSApp.activateIgnoringOtherApps_(True)
                return
            self._open_chat(data)
        return handler

    def _clear_history(self, _sender):
        self._history.clear()
        self._rebuild_history_menu()

    # ── Chat lifecycle ──────────────────────────────────────────────

    @objc.python_method
    def _open_chat(self, data):
        try:
            with open(PAUSE_FILE, "w") as f:
                f.write(str(int(time.time())))
        except Exception as e:
            print(f"[notify_server] pause-file write error: {e}")

        self._chat = ChatWindow.alloc().initWithData_onClose_(data, self._on_chat_closed)

    @objc.python_method
    def _on_chat_closed(self):
        self._chat = None
        if os.path.exists(PAUSE_FILE):
            try: os.remove(PAUSE_FILE)
            except: pass

    # ── Global hotkey handlers ──────────────────────────────────────
    # pynput delivers events on a background thread. Each handler bounces
    # to the main thread before doing any AppKit work.

    @objc.python_method
    def _hotkey_replay_all(self):
        AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(
            lambda: self._do_replay_all()
        )

    @objc.python_method
    def _hotkey_open_last_chat(self):
        AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(
            lambda: self._do_open_last_chat()
        )

    @objc.python_method
    def _do_replay_all(self):
        """Cmd+0 — show one toaster per persona that has any history,
        each one being that persona's most recent notification. Stacks
        oldest-first so the visually-newest is on top.

        Idempotent: first dismisses any currently-visible toasters, then
        spawns the fresh set. So mashing Cmd+0 always yields the same
        bounded number of toasters, never an accumulating pile."""
        if not self._history:
            return

        # 1. Clear any currently-visible toasters (including expanded ones).
        for entry in self._panels:
            try: entry["panel"].orderOut_(None)
            except: pass
        self._panels = []

        # 2. Find the most-recent entry per persona.
        most_recent = {}
        for entry in self._history:
            persona = entry["data"].get("persona", "casual")
            most_recent[persona] = entry  # later entries overwrite earlier

        # 3. Show in chronological order so the newest persona ends up on top.
        ordered = sorted(most_recent.values(), key=lambda e: e["displayed_at"])
        for entry in ordered:
            d = entry["data"]
            self._show_toaster(
                d.get("title", "TheSeer"),
                d.get("subtitle", "👁 TheSeer"),
                d.get("body", ""),
                d.get("persona", "casual"),
                record=False,    # already in history — don't double-record
            )

    # The next two are pure file writes — safe to call from the listener
    # thread directly, no main-thread bounce needed.

    @objc.python_method
    def _hotkey_sass(self):
        """Cmd+1 — secretly nudge the next tick into Sassy Seer."""
        self._write_override("sassy")

    @objc.python_method
    def _hotkey_random_persona(self):
        """Cmd+` — pick a random persona and let it own the next tick.
        Theatrical wildcard for demos."""
        chosen = random.choice(RANDOM_PERSONA_POOL)
        print(f"[notify_server] Cmd+` → next tick will route through: {chosen}")
        self._write_override(chosen)

    @objc.python_method
    def _hotkey_toggle_performance(self):
        """Cmd+2 — toggle 🎤 Performance mode without typing the phrase.
        theSeer.py picks up the flag on its next tick."""
        try:
            with open(PERF_TOGGLE_FILE, "w") as f:
                f.write(str(int(time.time())))
            print("[notify_server] Cmd+2 → performance-mode toggle queued")
        except Exception as e:
            print(f"[notify_server] perf-toggle write error: {e}")

    @objc.python_method
    def _write_override(self, persona):
        try:
            with open(OVERRIDE_FILE, "w") as f:
                f.write(persona)
        except Exception as e:
            print(f"[notify_server] override write error: {e}")

    @objc.python_method
    def _do_open_last_chat(self):
        """Cmd+9 — toggle the chat for the most recent notification.
        If a chat is already open, close it (so an accidental press
        can be undone with another press)."""
        if self._chat is not None:
            try:
                # close() fires windowWillClose_ which clears the pause file,
                # demotes activation policy, and nulls self._chat.
                self._chat.window.close()
            except Exception as e:
                print(f"[notify_server] chat close error: {e}")
            return
        if not self._history:
            return
        self._open_chat(self._history[-1]["data"])

    # ── Menu ────────────────────────────────────────────────────────

    @rumps.clicked("Quit")
    def quit_app(self, _):
        if self._hotkey_listener is not None:
            try: self._hotkey_listener.stop()
            except: pass
        for entry in self._panels:
            try: entry["panel"].orderOut_(None)
            except: pass
        for f in (FIFO, PAUSE_FILE, OVERRIDE_FILE, PERF_TOGGLE_FILE):
            if os.path.exists(f):
                try: os.remove(f)
                except: pass
        rumps.quit_application()


def _acquire_singleton_lock(path):
    """Try to atomically claim the lock file. Returns the open file handle
    (caller must keep it alive for the process lifetime) on success, or
    None if another instance already holds it.

    Uses fcntl.flock(LOCK_EX | LOCK_NB) which is automatically released
    when this process exits, even on crash — no stale-lock cleanup needed."""
    fd = open(path, "a+")  # append+read so we don't truncate before acquiring
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (IOError, OSError):
        fd.close()
        return None
    # Got the lock — record our PID
    fd.seek(0)
    fd.truncate()
    fd.write(str(os.getpid()))
    fd.flush()
    return fd


if __name__ == "__main__":
    _lock = _acquire_singleton_lock(LOCK_FILE)
    if _lock is None:
        try:
            with open(LOCK_FILE) as _f:
                existing_pid = _f.read().strip() or "?"
        except Exception:
            existing_pid = "?"
        print(f"[notify_server] another instance is already running (PID {existing_pid}). "
              f"Exiting.\n  To stop the existing one:  kill {existing_pid}",
              file=sys.stderr, flush=True)
        sys.exit(1)

    TheSeerNotifyServer().run()
