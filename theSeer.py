"""
TheSeer Logic Engine
====================

Each tick:
  1. Pull recent OCR records from screenpipe.
  2. Decide the active persona (auditor > app-driven > casual).
  3. Filter context, run a "confidence" LLM call: produces a tip + 0.0–1.0 score.
  4. Run a "priority" LLM call: scores how worthwhile it is to push this tip
     given the user's recently-sent notifications (novelty / importance).
  5. Final score = average(confidence, priority).
  6. If final score ≥ NOTIFICATION_THRESHOLD (or auditor) → push a banner.
  7. Print a card-style entry to the terminal showing every component of
     the decision — 🔊 sent, 🔇 suppressed, or ⏭ skipped.

Test mode (--test_mode) replaces both LLM calls with deterministic local
classifiers so the whole pipeline (screenpipe → persona → scoring →
notification) can be verified without the MLX server running.
"""

import argparse
import base64
import difflib
import fcntl
import json
import os
import re
import subprocess
import sys
import textwrap
import time
from collections import deque
from datetime import datetime, timezone, timedelta

import requests

import configuration as cfg
import personas as p


# ──────────────────────────────────────────────────────────────────
# Test-mode helpers (no LLM)
# ──────────────────────────────────────────────────────────────────

def simple_classifier(persona, apps_seen, context):
    """Test-mode confidence call. Returns (confidence 0.0–1.0, tip)."""
    app_list = ", ".join(sorted(apps_seen)) if apps_seen else "none"
    word_count = len(context.split())
    if persona == "auditor":
        triggered = [kw for kw in p.PERSONAS["auditor"]["trigger_keywords"]
                     if kw.lower() in context.lower()]
        kw_hit = triggered[0] if triggered else "unknown keyword"
        return 1.0, f"[TEST] 🚨 Auditor triggered by: '{kw_hit}' — apps: {app_list}"
    icons = {
        "entertainer": "🎭", "teacher": "📚", "motivator": "💪",
        "sassy":       "🌶️", "performance": "🎤",
    }
    if persona in ("entertainer", "teacher", "motivator", "sassy"):
        triggered = [kw for kw in p.PERSONAS.get(persona, {}).get("trigger_keywords", [])
                     if kw.lower() in context.lower()]
        kw_hit = triggered[0] if triggered else f"{persona} request"
        return 1.0, f"[TEST] {icons.get(persona, '✨')} {persona.title()} fired by '{kw_hit}' — would respond using: {app_list}"
    if persona == "performance":
        return 1.0, f"[TEST] {icons['performance']} Performance mode — would rhyme about: {app_list}"
    messages = {
        "engineer":  f"[TEST] 🛠 Engineer mode — {word_count} words of code from: {app_list}",
        "executive": f"[TEST] 💼 Executive mode — drafting detected in: {app_list}",
        "casual":    f"[TEST] 👁 Casual mode — {word_count} words seen, apps: {app_list}",
    }
    return 0.8, messages.get(persona, f"[TEST] Persona: {persona}, apps: {app_list}")


def simple_priority(tip, sent_history):
    """Test-mode priority call. Lower = more redundant with recent sends."""
    if not sent_history:
        return 0.9
    if tip == sent_history[-1]:
        return 0.15
    tip_words = set(tip.lower().split())
    if not tip_words:
        return 0.3
    for past in list(sent_history)[-3:]:
        past_words = set(past.lower().split())
        if past_words:
            overlap = len(tip_words & past_words) / max(len(tip_words), 1)
            if overlap > 0.7:
                return 0.35
            if overlap > 0.4:
                return 0.6
    return 0.85


# ──────────────────────────────────────────────────────────────────
# Card-style terminal rendering
# ──────────────────────────────────────────────────────────────────

CARD_W = 76

_STATUS = {
    "sent":       "🔊 sent",
    "suppressed": "🔇 suppressed",
    "skipped":    "⏭  skipped",
    "paused":     "⏸  paused",
    "ready":      "✅ ready",
}


def _header(label):
    text = f"┌─ {label} "
    return text + "─" * max(3, CARD_W - len(text))


def _footer():
    return "└" + "─" * (CARD_W - 1)


def _append(target, text="", indent=3):
    pad = " " * indent
    if not text:
        target.append("│")
        return
    for line in text.split("\n"):
        wrapped = textwrap.wrap(
            line,
            width=CARD_W - indent - 1,
            break_long_words=False,
            break_on_hyphens=False,
        ) or [""]
        for w in wrapped:
            target.append("│" + pad + w)


def render_card(persona, ts, *, status,
                apps=None, confidence=None, priority=None, final_score=None,
                tip=None, sent_payload=None, threshold=None, skip_reason=None,
                suppress_reason=None, context_preview=None, used_image=False):
    """Render one tick as a vertically-stacked card. Always prints something."""
    label = f"{ts} · {persona.upper()} · {_STATUS[status]}"
    lines = [_header(label)]

    if status == "ready":
        _append(lines, tip or "TheSeer is online.")
        lines.append(_footer())
        print("\n".join(lines), flush=True)
        return

    if status == "skipped":
        _append(lines, f"⏭  {skip_reason or '(no reason)'}")
        lines.append(_footer())
        print("\n".join(lines), flush=True)
        return

    if status == "paused":
        _append(lines, f"⏸  {skip_reason or 'paused'}")
        lines.append(_footer())
        print("\n".join(lines), flush=True)
        return

    # Scored ticks (sent or suppressed): full breakdown
    apps_str = ", ".join(sorted(apps)) if apps else "—"
    # 📸 marks ticks where a screenshot was captured and sent to the model.
    apps_line = f"apps:    {apps_str}"
    if used_image:
        apps_line += "   📸 screenshot sent"
    _append(lines, apps_line)
    if context_preview:
        _append(lines, f"seen:    \"{context_preview}\"")
    if confidence is not None and priority is not None and final_score is not None:
        _append(lines,
                f"scores:  conf {confidence:.2f}  ·  priority {priority:.2f}  ·  final {final_score:.2f}")
    _append(lines)
    _append(lines, f"💬 {tip}" if tip else "💬 (empty)")
    _append(lines)

    if status == "sent" and sent_payload:
        title, subtitle, body = sent_payload
        _append(lines, f"📨 {title}")
        _append(lines, subtitle, indent=7)
        _append(lines, body,    indent=7)
    else:
        # Suppressed — show the actual reason, not a hard-coded template.
        if suppress_reason:
            _append(lines, f"🚫 {suppress_reason}")
        else:
            thr_str = f"{threshold:.2f}" if threshold is not None else "?"
            score_str = f"{final_score:.2f}" if final_score is not None else "?"
            _append(lines, f"🚫 suppressed (final {score_str}, threshold {thr_str})")

    lines.append(_footer())
    print("\n".join(lines), flush=True)


# ──────────────────────────────────────────────────────────────────
# Main class
# ──────────────────────────────────────────────────────────────────

class TheSeer:
    FIFO_PATH  = "/tmp/theseer_notify"
    PAUSE_PATH = "/tmp/theseer_chat_active"  # set by notify_server while a chat is open

    def __init__(self, test_mode=False):
        self.current_persona       = "casual"
        self.last_notified_persona = ""
        self.all_clear_count       = 0
        self.test_mode             = test_mode
        self.sent_history          = deque(maxlen=10)
        # Per-persona dedup: most recent tip text that each persona actually
        # sent. Stops the *same* persona from repeating itself, while still
        # letting different personas independently surface their own ideas.
        self._last_tip_by_persona  = {}
        # Per-persona cooldown tracking for any one-shot persona.
        # Keys are persona names, values are monotonic timestamps.
        self._one_shot_last_fired  = {}
        # Performance mode is a sticky toggle (entered/exited by phrases).
        self._performance_mode     = False
        # Last time the quiet-screen ambient call fired (monotonic). Used to
        # rate-limit ambient chatter so an idle screen stays mostly silent.
        self._last_ambient_at      = 0.0
        # When a hotkey override fires (Cmd+1, Cmd+`), the next tick uses
        # the overridden persona AND bypasses the score threshold. This
        # flag is set in determine_persona and cleared at the start of the
        # following tick.
        self._overridden_this_tick = False
        # Change detection: normalized active-window text from the previous tick.
        # Analytical personas skip re-analysing a screen that hasn't changed,
        # which stops repetition and saves a model call on a static screen.
        self._last_screen_text     = ""

        msg = "TheSeer is watching your screen. Notifications are working!"
        if test_mode:
            msg = "[TEST MODE] " + msg

        # Startup banner — pushes through the same path so we can confirm
        # visually that everything is wired up. `meta=True` keeps it out of
        # the Cmd+0 replay history (it's a system ping, not a real tip).
        payload = self.notify("👁️ The Seer is Watching", msg, is_critical=False, meta=True)
        render_card(
            self.current_persona,
            datetime.now().strftime("%H:%M:%S"),
            status="ready",
            tip=msg,
            sent_payload=payload,
        )

    # ── logging / notifications ────────────────────────────────────

    def log(self, message):
        """Unstructured info / errors (the cards handle tick output)."""
        print(f"[{datetime.now().strftime('%H:%M:%S')}] [{self.current_persona.upper()}] {message}",
              flush=True)

    def notify(self, title, message, is_critical=False, meta=False):
        """Non-blocking write to the FIFO consumed by notify_server.py.
        `meta=True` flags system notifications (e.g. startup ping) so that
        notify_server does NOT record them into the History / Cmd+0 replay
        pool.

        Returns the (title, subtitle, body) actually delivered, or None."""
        clean_msg = " ".join(message.split())
        # Safety net only — a well-behaved tip is 1–2 sentences and fits easily.
        # Cut on a word boundary so we never chop mid-word (the ugly "…proc…").
        BODY_LIMIT = 320
        if len(clean_msg) > BODY_LIMIT:
            clean_msg = clean_msg[:BODY_LIMIT].rsplit(" ", 1)[0].rstrip(" ,.;:—-") + "…"
        subtitle = "🚨 CRITICAL" if is_critical else "👁 TheSeer"
        payload  = json.dumps({
            "title":    title,
            "subtitle": subtitle,
            "body":     clean_msg,
            "persona":  self.current_persona,   # so the chat can use the right system prompt
            "meta":     meta,
        })
        try:
            # O_NONBLOCK so we don't hang if notify_server.py is down
            fd = os.open(self.FIFO_PATH, os.O_WRONLY | os.O_NONBLOCK)
            try:
                os.write(fd, (payload + "\n").encode())
            finally:
                os.close(fd)
            return (title, subtitle, clean_msg)
        except OSError as e:
            self.log(f"⚠️  Notification dropped — notify_server.py not running? ({e})")
        except Exception as e:
            self.log(f"⚠️  Notification error: {e}")
        return None

    # ── persona switching ──────────────────────────────────────────

    # Personas that are one-shot (fire once then revert to a base persona).
    # Order matters when multiple triggers match — first one wins.
    ONE_SHOT_PERSONAS = ("entertainer", "teacher", "motivator", "sassy")

    # Personas that are sticky modes — entered/exited by phrases, persistent
    # across ticks while active.
    STICKY_PERSONAS = ("performance",)

    # Personas that should bypass NOTIFICATION_THRESHOLD because the user
    # explicitly asked for them.
    USER_INITIATED_PERSONAS = ONE_SHOT_PERSONAS + STICKY_PERSONAS

    # File the notify_server writes to when a hotkey override (Cmd+1, Cmd+`)
    # wants the next tick to use a specific persona.
    OVERRIDE_FILE     = "/tmp/theseer_next_persona"
    # File notify_server touches when Cmd+2 is pressed to toggle perf mode.
    PERF_TOGGLE_FILE  = "/tmp/theseer_perf_toggle"
    # File notify_server writes per-persona engage/dismiss tallies to. We read
    # it (read-only) to nudge each persona's notification threshold: personas
    # the user keeps dismissing get a higher bar (speak less); personas they
    # click through to chat get a lower bar (speak more).
    FEEDBACK_FILE     = "/tmp/theseer_feedback.json"
    # How far feedback can move a persona's threshold, up or down.
    FEEDBACK_MAX_ADJ  = 0.2
    # Engage/dismiss signals needed before the nudge reaches full strength —
    # keeps a single stray click from swinging the bar wildly.
    FEEDBACK_FULL_AT  = 5

    def determine_persona(self, raw_text_all_apps, apps_seen, focused_text=""):
        context_lower = raw_text_all_apps.lower()
        # User-intent triggers match against the focused app's text only (see
        # check_screen). Falls back to the full blob if we couldn't identify a
        # focused app, so behaviour degrades to the old whole-screen matching.
        focused_lower = (focused_text or raw_text_all_apps).lower()
        apps_lower    = {a.lower() for a in apps_seen}

        # Clear the "we used an override last tick" flag at the start of
        # every tick. It only stays True for the duration of the override
        # tick itself (set in the override branch below).
        self._overridden_this_tick = False

        # 0. Hotkey override (Cmd+1 / Cmd+`) — written by notify_server.
        # Takes precedence over everything except security, which we still
        # respect by skipping the override if Auditor would fire.
        override = self._consume_override_file()
        if override:
            auditor_kws = p.PERSONAS["auditor"]["trigger_keywords"]
            if not any(k.lower() in context_lower for k in auditor_kws):
                self._overridden_this_tick = True
                return override
            # else: a secret is on screen — security wins, drop the override.

        # 1. Coming OUT of any one-shot persona: revert to default.
        if self.current_persona in self.ONE_SHOT_PERSONAS:
            return self._default_persona(apps_lower)

        # 2. Performance-mode enter/exit phrase check (sticky toggle).
        #    Scoped to the focused app — it's a user-intent phrase.
        self._maybe_toggle_performance(focused_lower)

        # 3. Security keywords always win.
        if any(k.lower() in context_lower
               for k in p.PERSONAS["auditor"]["trigger_keywords"]):
            self.all_clear_count = 0
            return "auditor"

        # 4. Require N clean ticks before leaving auditor mode.
        if self.current_persona == "auditor":
            self.all_clear_count += 1
            if self.all_clear_count < cfg.REVERT_THRESHOLD:
                self.log(f"🔐 No keywords — clean tick {self.all_clear_count}/{cfg.REVERT_THRESHOLD}")
                return "auditor"
            self.all_clear_count = 0

        # 5. One-shot personas (cooldown-protected). First matching trigger wins.
        #    Matched against the focused app's text only — a coworker's "explain
        #    this" in a background Slack window shouldn't fire Teacher.
        now = time.monotonic()
        for one_shot in self.ONE_SHOT_PERSONAS:
            kws = p.PERSONAS.get(one_shot, {}).get("trigger_keywords", [])
            if not kws:
                continue
            if not any(k.lower() in focused_lower for k in kws):
                continue
            cooldown = self._one_shot_cooldown(one_shot)
            last     = self._one_shot_last_fired.get(one_shot, 0.0)
            if (now - last) > cooldown:
                self._one_shot_last_fired[one_shot] = now
                return one_shot

        # 6. Default: performance if active, else app-based base.
        return self._default_persona(apps_lower)

    @staticmethod
    def _one_shot_cooldown(persona_name):
        """Look up `<NAME>_COOLDOWN_SEC` in configuration.py, default 300."""
        attr = f"{persona_name.upper()}_COOLDOWN_SEC"
        return getattr(cfg, attr, 300)

    def _consume_override_file(self):
        """Read + delete the hotkey-override file written by notify_server.
        Returns the persona name (if valid) or None."""
        path = self.OVERRIDE_FILE
        if not os.path.exists(path):
            return None
        try:
            with open(path) as f:
                name = f.read().strip()
            os.remove(path)
        except Exception:
            return None
        if name and name in p.PERSONAS:
            self.log(f"🎲 hotkey override → {name}")
            return name
        return None

    def _maybe_toggle_performance(self, context_lower):
        """Toggle performance mode based on (a) the Cmd+2 hotkey file
        written by notify_server, or (b) the enter/exit phrases visible on
        screen. Hotkey wins if both are queued in the same tick."""
        # (a) Cmd+2 hotkey toggle
        if os.path.exists(self.PERF_TOGGLE_FILE):
            try:
                os.remove(self.PERF_TOGGLE_FILE)
                self._performance_mode = not self._performance_mode
                self.log(f"🎤 performance mode {'ON' if self._performance_mode else 'OFF'} (Cmd+2)")
                return
            except Exception:
                pass

        # (b) Phrase-based toggle
        perf = p.PERSONAS.get("performance", {})
        if self._performance_mode:
            for phrase in perf.get("exit_phrases", []):
                if phrase in context_lower:
                    self._performance_mode = False
                    self.log("🎤 performance mode OFF (phrase)")
                    return
        else:
            for phrase in perf.get("enter_phrases", []):
                if phrase in context_lower:
                    self._performance_mode = True
                    self.log("🎤 performance mode ON (phrase)")
                    return

    def _default_persona(self, apps_lower):
        """The persona that would naturally apply right now: performance if
        sticky-active, otherwise app-based base."""
        if self._performance_mode:
            return "performance"
        return self._base_persona(apps_lower)

    def _base_persona(self, apps_lower):
        """Return the persona dictated purely by which apps are visible."""
        excluded = ("auditor",) + self.ONE_SHOT_PERSONAS + self.STICKY_PERSONAS
        for name, data in p.PERSONAS.items():
            if name in excluded:
                continue
            triggers = [t.lower() for t in data.get("trigger_apps", [])]
            if any(a in triggers for a in apps_lower):
                return name
        return "casual"

    # ── Screen capture ─────────────────────────────────────────────

    SCREENSHOT_PATH = "/tmp/theseer_frame.jpg"

    @staticmethod
    def _frontmost_window_id():
        """CGWindowID of the focused app's frontmost real window, or None.

        screencapture works on display indices or window ids, not app names, so
        we ask the window server: take the active app's pid, then the first
        on-screen, normal-layer (0) window of meaningful size — the window list
        is already front-to-back z-order, so that's the focused window."""
        try:
            import Quartz
            from AppKit import NSWorkspace
            app = NSWorkspace.sharedWorkspace().frontmostApplication()
            if app is None:
                return None
            pid  = app.processIdentifier()
            opts = (Quartz.kCGWindowListOptionOnScreenOnly
                    | Quartz.kCGWindowListExcludeDesktopElements)
            for w in Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID):
                if w.get("kCGWindowOwnerPID") != pid:
                    continue
                if w.get("kCGWindowLayer", 0) != 0:   # skip menubar/panels/overlays
                    continue
                b = w.get("kCGWindowBounds", {})
                if b.get("Width", 0) < 100 or b.get("Height", 0) < 100:
                    continue
                return int(w.get("kCGWindowNumber"))
        except Exception:
            pass
        return None

    def _capture_screenshot(self):
        """Grab the focused window (falling back to the main display), downscale,
        and return a base64 JPEG data URI (the format mlx_vlm's server accepts).
        Returns None on any failure so the caller degrades gracefully to OCR-only.

        Capturing just the active window sharpens grounding (the model isn't
        distracted by background apps) and works regardless of which monitor the
        window is on. Uses only macOS built-ins — no Python image deps."""
        try:
            wid = self._frontmost_window_id()
            if wid is not None:
                # -o: omit the window's drop shadow. -l: capture that window id.
                cmd = ["screencapture", "-x", "-o", "-t", "jpg", "-l", str(wid),
                       self.SCREENSHOT_PATH]
            else:
                # -D 1: main display only (avoids huge multi-monitor grabs).
                cmd = ["screencapture", "-x", "-t", "jpg", "-D", "1",
                       self.SCREENSHOT_PATH]
            subprocess.run(cmd, check=True, timeout=5, capture_output=True)
            # Cap the longest side so the payload + vision encoding stay fast.
            max_px = getattr(cfg, "SCREENSHOT_MAX_PX", 1400)
            subprocess.run(
                ["sips", "-Z", str(max_px), self.SCREENSHOT_PATH],
                check=True, timeout=5, capture_output=True,
            )
            with open(self.SCREENSHOT_PATH, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            return f"data:image/jpeg;base64,{b64}"
        except Exception as e:
            self.log(f"⚠️  screenshot capture failed — falling back to OCR-only: {e}")
            return None

    # ── LLM calls ──────────────────────────────────────────────────

    @staticmethod
    def _strip_think(text):
        """Drop chain-of-thought some models emit. If a </think> is present,
        the real answer is whatever follows the last one; then remove any
        stray <think>/</think> tags that slipped through."""
        if "</think>" in text.lower():
            text = re.split(r"</think>", text, flags=re.IGNORECASE)[-1]
        return re.sub(r"</?think>", "", text, flags=re.IGNORECASE).strip()

    def _confidence_call(self, persona_data, context, image=None, recent_tips=None):
        """First LLM call: persona-flavoured tip + confidence score.

        Two persona families need different framing:
          • ANALYTICAL (casual, engineer, executive, auditor) — examine the
            screen and surface a concrete, useful observation/tip, self-scored
            for confidence. Gets the strict output contract below.
          • EXPRESSIVE (sassy, performance, teacher, entertainer, motivator) —
            REACT to the screen in character (sass, a verse, an analogy, a
            joke) and output only that. Forcing the analytical contract on them
            flattened their voice and made them narrate, so they get a minimal
            prompt and a fixed confidence (they're user-requested and bypass the
            score gate anyway).
        """
        expressive = bool(persona_data.get("expressive"))

        if expressive:
            system_msg = (
                f"{persona_data['prompt']} {persona_data['negative_prompt']}\n\n"
                "The text below is what's on the user's screen right now — use it as raw "
                "material to react to, but never describe, summarize, or narrate it. Stay "
                "FULLY in character and output ONLY your response (your joke / verse / sassy "
                "line / lesson) — nothing else. No preamble, no labels, no score, no "
                "explanation, no <think> tags."
            )
            if image:
                system_msg += (
                    " A screenshot is also provided so you can see what's on screen — react "
                    "to it in character; do not describe it."
                )
        else:
            system_msg = (
                f"{persona_data['prompt']} {persona_data['negative_prompt']}\n\n"
                "CONTEXT: screenpipe captures every open window. If the context marks an "
                "ACTIVE WINDOW, that is what the user is focused on right now — focus there and "
                "treat any BACKGROUND WINDOWS as supporting context only.\n\n"
                "OUTPUT FORMAT (strict):\n"
                "1. Begin with `[SCORE: X.X]` where X.X is your confidence "
                "(0.0 = routine / not worth mentioning, 1.0 = critical / urgent).\n"
                "2. Follow with a single space, then your message — a single, clean, useful "
                "tip in your role's voice. One sentence, two at most.\n"
                "3. Your message is shown DIRECTLY to the user as a notification. No preamble "
                "(`Here's a tip:`, `Sure!`) and no meta-commentary (`I see you're…`, `Looking "
                "at your screen…`). Never name or narrate the screen, window, or app (`the "
                "active window is…`, `the terminal shows…`, `in VS Code…`); the app label in "
                "the context only helps you understand what you're looking at — it is NOT "
                "necessarily where the content came from (e.g. a coding assistant running "
                "inside a terminal). Give the advice itself, as if you already know the topic.\n"
                "4. Output ONLY the final message. Do not emit reasoning, planning, or <think> tags."
            )
            if image:
                system_msg += (
                    "\n\nYou are also given a screenshot of the user's screen. Use BOTH the "
                    "screenshot and the OCR text to ground your response in what's actually "
                    "visible — layout, what's focused, diagrams, highlighted errors. Still "
                    "never narrate the screen; just deliver your message."
                )
            # Session memory: surface what we've already said so the model picks
            # a genuinely new angle instead of rephrasing a recent tip.
            if recent_tips:
                recent = "\n".join(f"  • {t}" for t in recent_tips[-5:])
                system_msg += (
                    "\n\nYou have ALREADY given the user these tips recently — do NOT repeat "
                    "them or their topics; surface something new:\n" + recent
                )

        # Build the user turn. When we have a screenshot, send it in the
        # OpenAI vision format (a content-parts list) so the model can ground
        # its response in actual layout/focus/visuals, not just OCR text.
        user_text = f"Screen context: {context}"
        if image:
            user_content = [
                {"type": "text",      "text": user_text},
                {"type": "image_url", "image_url": {"url": image}},
            ]
        else:
            user_content = user_text

        try:
            response = requests.post(
                cfg.MLX_SERVER_URL,
                json={
                    "model": cfg.MODEL_ID,
                    "messages": [
                        {"role": "system", "content": system_msg},
                        {"role": "user",   "content": user_content},
                    ],
                    "max_tokens": cfg.MAX_TOKENS,
                },
                # Vision encoding is slower than text-only — give it more room.
                timeout=30 if image else 15,
            )
        except Exception as e:
            self.log(f"⚠️  MLX confidence call failed: {e}")
            return None, None

        if response.status_code != 200:
            self.log(f"⚠️  MLX returned {response.status_code}")
            return None, None
        choices = response.json().get("choices")
        if not choices:
            return None, None

        raw   = self._strip_think(choices[0]["message"]["content"].strip())
        # Strip a [SCORE: X.X] prefix if present. Analytical personas are asked
        # to produce one; expressive personas aren't, but we strip it defensively
        # in case one leaks through, so it never pollutes the displayed body.
        # Case-insensitive — the model sometimes writes [Score: 1.0]/[score: 1.0].
        parsed = 1.0
        m      = re.match(r"\[SCORE:\s*([0-9]*\.?[0-9]+)\]", raw, re.IGNORECASE)
        if m:
            parsed = min(1.0, max(0.0, float(m.group(1))))
            tip    = raw[m.end():].strip()
        else:
            tip    = raw
        # Expressive personas are user-requested and bypass the score gate, so
        # their self-score is meaningless — use a fixed confidence instead.
        score = 0.9 if expressive else parsed
        return score, tip

    def _ambient_call(self):
        """When the screen is quiet and no real context is available, casual
        still pipes up. Lighter prompt with no screen context — produces a
        small ambient observation, tip, or fact."""
        if self.test_mode:
            return 0.7, "[TEST] 🌙 Quiet screen — perfect time to stretch and drink water."

        system_msg = (
            "You are a casual ambient assistant. The user's screen is currently quiet — "
            "nothing actionable to comment on. Share ONE brief, interesting thought: a tip, "
            "a fun fact, a productivity nudge, a small reminder.\n\n"
            "OUTPUT FORMAT (strict): the text you write is shown DIRECTLY to the user as a "
            "notification. One clean sentence. No preamble like 'Here's a thought'. No meta "
            "like 'your screen is quiet'.\n\n"
            "Examples:\n"
            "  • Quiet moment — a good time to stand up and stretch.\n"
            "  • Did you know? The first computer bug was an actual moth, found in 1947 in a Harvard relay.\n"
            "  • If you've been at this for an hour, drink some water.\n"
            "  • Two minutes of focused breathing resets attention better than ten minutes of doomscrolling."
        )
        try:
            response = requests.post(
                cfg.MLX_SERVER_URL,
                json={
                    "model": cfg.MODEL_ID,
                    "messages": [
                        {"role": "system", "content": system_msg},
                        {"role": "user",   "content": "Give me one brief ambient thought."},
                    ],
                    "max_tokens": cfg.MAX_TOKENS,
                },
                timeout=15,
            )
            if response.status_code != 200:
                return None, None
            content = self._strip_think(
                response.json()["choices"][0]["message"]["content"].strip())
            # Ambient calls don't return a [SCORE: X.X] prefix — fixed confidence.
            return 0.7, content
        except Exception as e:
            self.log(f"⚠️  Ambient call failed: {e}")
            return None, None

    def _effective_threshold(self, persona):
        """Base NOTIFICATION_THRESHOLD nudged by how the user reacts to THIS
        persona. Engage (click→chat) lowers the bar so it speaks more often;
        dismiss (✕) raises it so it speaks less. Auto-timeouts are neutral and
        ignored here. Bounded to ±FEEDBACK_MAX_ADJ and ramped in by sample size
        so one stray signal can't swing it. Read-only; returns the base on any
        problem so the engine never breaks over feedback bookkeeping."""
        base = cfg.NOTIFICATION_THRESHOLD
        try:
            with open(self.FEEDBACK_FILE) as f:
                row = json.load(f).get(persona)
            if not row:
                return base
            pos = row.get("positive", 0)
            neg = row.get("negative", 0)
            decisive = pos + neg
            if decisive == 0:
                return base
            rate     = (pos - neg) / decisive                  # [-1, +1]
            strength = min(decisive / self.FEEDBACK_FULL_AT, 1.0)
            adj      = -rate * self.FEEDBACK_MAX_ADJ * strength  # +engage → lower bar
            return max(0.0, min(1.0, base + adj))
        except Exception:
            return base

    def _priority_call(self, tip):
        """Second LLM call: rate THIS tip's worth against recently-sent ones."""
        if self.test_mode:
            return simple_priority(tip, self.sent_history)

        history_lines = list(self.sent_history)[-5:]
        history_text  = "\n".join(f"- {h}" for h in history_lines) or "(nothing yet)"
        system_msg = (
            "You rate notification priority for an ambient assistant. "
            "Output ONLY one number between 0.0 and 1.0 — no words, no units, no explanation. "
            "Use the FULL range honestly:\n"
            "  - 0.0-0.2 = near-duplicate of something already sent, or ambient/background\n"
            "  - 0.3-0.5 = mild observation, not really actionable\n"
            "  - 0.6-0.8 = useful and timely, worth interrupting for\n"
            "  - 0.9-1.0 = genuinely urgent, novel, or critical (security exposure, etc.)\n"
            "Most tips should land in 0.3-0.7. Reserve 0.9+ for truly important moments."
        )
        user_msg = (
            f"Proposed notification:\n{tip}\n\n"
            f"Recently sent (most recent at bottom):\n{history_text}\n\n"
            "Priority score (0.0–1.0):"
        )
        try:
            response = requests.post(
                cfg.MLX_SERVER_URL,
                json={
                    "model": cfg.MODEL_ID,
                    "messages": [
                        {"role": "system", "content": system_msg},
                        {"role": "user",   "content": user_msg},
                    ],
                    "max_tokens": 8,
                },
                timeout=10,
            )
            if response.status_code != 200:
                return 0.5
            content = response.json()["choices"][0]["message"]["content"]
            m = re.search(r"([0-9]*\.?[0-9]+)", content)
            if m:
                return min(1.0, max(0.0, float(m.group(1))))
        except Exception as e:
            self.log(f"⚠️  Priority call failed: {e}")
        return 0.5

    # ── main loop step ─────────────────────────────────────────────

    def check_screen(self):
        ts = datetime.now().strftime("%H:%M:%S")

        # If a chat is currently open, skip inference — avoids competing with
        # the chat for the MLX server and respects the user's focus.
        if os.path.exists(self.PAUSE_PATH):
            render_card(self.current_persona, ts, status="paused",
                        skip_reason="chat is open — inference paused")
            return

        try:
            headers = {"Authorization": f"Bearer {cfg.SCREENPIPE_TOKEN}"}
            params  = {"limit": cfg.CONTEXT_LIMIT, "content_type": "ocr"}
            response = requests.get(cfg.SCREENPIPE_URL, headers=headers, params=params, timeout=5)
            if response.status_code != 200:
                render_card(self.current_persona, ts, status="skipped",
                            skip_reason=f"screenpipe API status {response.status_code}")
                return

            data = response.json().get("data", [])
            if not data:
                render_card(self.current_persona, ts, status="skipped",
                            skip_reason="waiting for screen data…")
                return

            # ── Recency filter ─────────────────────────────────────────
            # Only consider OCR records captured within RECENCY_WINDOW_SEC
            # of now. Prevents notifications about a window you switched
            # away from a few seconds ago.
            recency_sec = getattr(cfg, "RECENCY_WINDOW_SEC", 8)
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=recency_sec)
            fresh = []
            for item in data:
                ts_str = item.get("content", {}).get("timestamp", "")
                if not ts_str:
                    continue
                try:
                    when = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    if when.tzinfo is None:
                        when = when.replace(tzinfo=timezone.utc)
                except Exception:
                    continue
                if when >= cutoff:
                    fresh.append((when, item))

            # Empty `fresh` is OK — we let the flow continue. Casual will
            # invoke the ambient call later; other personas will skip when
            # they discover no context to work with.
            # Newest-first so app_records[0] is the most recently captured
            # window — don't rely on screenpipe's response ordering.
            fresh.sort(key=lambda pair: pair[0], reverse=True)

            apps_seen, app_records = set(), []
            for _when, item in fresh:
                app  = item["content"].get("app_name", "Unknown")
                text = item["content"].get("text", "")
                if text.strip():
                    app_records.append((app, text))
                    apps_seen.add(app)

            raw_text     = " ".join(f"[{a}]: {t}" for a, t in app_records)
            # app_records[0] is now the most recently captured window — the app
            # the user is most likely focused on. We scope user-intent keyword
            # triggers ("be sassy", "explain this", …) to THAT app's text only,
            # so the same phrase in a background window or someone else's message
            # doesn't spuriously fire a persona. (Auditor still scans everything.)
            focused_app  = app_records[0][0] if app_records else None
            focused_text = " ".join(t for a, t in app_records if a == focused_app)
            new_persona  = self.determine_persona(raw_text, apps_seen, focused_text)
            if new_persona != self.current_persona:
                self.log(f"🔄 persona: {self.current_persona} → {new_persona}")
                self.current_persona = new_persona

            persona_data = p.PERSONAS[self.current_persona]
            excluded     = [a.lower() for a in persona_data.get("exclude_apps", [])]

            # screenpipe captures EVERY open window, but the active window is
            # what the user is actually working in — so we present it as the
            # primary subject and demote the rest to background context. The
            # active window also gets first claim on the character budget.
            CONTEXT_BUDGET = 1200
            active_text = (focused_text.strip()
                           if focused_app and focused_app.lower() not in excluded else "")
            other_parts = [f"[{a}]: {t}" for a, t in app_records
                           if a != focused_app and a.lower() not in excluded]
            other_text  = " ".join(other_parts).strip()

            if active_text:
                context = f"ACTIVE WINDOW — {focused_app} (the user is focused here):\n{active_text}"
                remaining = CONTEXT_BUDGET - len(context)
                if other_text and remaining > 120:
                    context += ("\n\nBACKGROUND WINDOWS (other open apps — context only):\n"
                                + other_text[:remaining - 80])
            else:
                # Focused app was excluded for this persona (or unknown) — fall
                # back to the background blob as the working context.
                context = other_text
            context = context[:CONTEXT_BUDGET]

            # Tracks whether a screenshot was successfully captured AND handed
            # to the model this tick. Surfaced as a 📸 on the terminal card.
            used_image = False

            if context.strip():
                # Normal flow — there's something to look at.
                preview = re.sub(r"\s+", " ", context).strip()
                if len(preview) > 100:
                    preview = preview[:97] + "..."

                # ── Change detection ──────────────────────────────────
                # An analytical persona has nothing new to add when the active
                # window hasn't meaningfully changed since last tick — skip the
                # model call entirely (stops repetition, saves compute). Auditor
                # always re-scans for secrets; user-requested and expressive
                # personas were explicitly asked for, so they're exempt.
                cur_screen = re.sub(r"\s+", " ", active_text or context).strip()
                prev_screen = self._last_screen_text
                self._last_screen_text = cur_screen   # refresh baseline every tick
                analytical = not persona_data.get("expressive")
                if (analytical and self.current_persona != "auditor"
                        and not self._overridden_this_tick and prev_screen
                        and difflib.SequenceMatcher(None, cur_screen, prev_screen).ratio() >= 0.92):
                    render_card(self.current_persona, ts, status="skipped",
                                skip_reason="screen unchanged since last look")
                    return

                if self.test_mode:
                    confidence, tip = simple_classifier(self.current_persona, apps_seen, context)
                else:
                    image = self._capture_screenshot() if cfg.SEND_SCREENSHOTS else None
                    used_image = image is not None
                    confidence, tip = self._confidence_call(
                        persona_data, context, image=image,
                        recent_tips=list(self.sent_history))
                    if confidence is None or tip is None:
                        render_card(self.current_persona, ts, status="skipped",
                                    skip_reason="LLM confidence call failed")
                        return
            elif self.current_persona == "casual":
                # Quiet screen. An ambient assistant that talks every idle tick
                # becomes noise — so we hold our tongue and only pipe up once
                # per AMBIENT_QUIET_GAP_SEC of genuine quiet.
                now = time.monotonic()
                gap = getattr(cfg, "AMBIENT_QUIET_GAP_SEC", 1800)
                since = now - self._last_ambient_at
                if since < gap:
                    mins_left = int((gap - since) // 60)
                    render_card(self.current_persona, ts, status="skipped",
                                skip_reason=f"quiet screen — staying silent (~{mins_left}m until next ambient)")
                    return
                self._last_ambient_at = now
                preview = "(quiet — no recent screen activity)"
                confidence, tip = self._ambient_call()
                if confidence is None or tip is None:
                    render_card(self.current_persona, ts, status="skipped",
                                skip_reason="ambient call failed")
                    return
            elif self.current_persona in self.USER_INITIATED_PERSONAS or self._overridden_this_tick:
                # User explicitly asked for this persona (one-shot trigger, hotkey
                # override, or sticky mode). Give them something even without
                # screen context — the persona prompts already say "riff on
                # whatever's visible, or just deliver any joke / quote / etc."
                preview = "(no recent screen activity — user-initiated)"
                if self.test_mode:
                    confidence, tip = simple_classifier(self.current_persona, apps_seen, "")
                else:
                    image = self._capture_screenshot() if cfg.SEND_SCREENSHOTS else None
                    used_image = image is not None
                    confidence, tip = self._confidence_call(
                        persona_data,
                        "(no on-screen text captured — the user explicitly requested you; "
                        "use the screenshot if it helps, otherwise just deliver your content "
                        "without referencing the screen)",
                        image=image,
                        recent_tips=list(self.sent_history),
                    )
                    if confidence is None or tip is None:
                        render_card(self.current_persona, ts, status="skipped",
                                    skip_reason=f"LLM call failed for {self.current_persona}")
                        return
            else:
                # App-driven persona with no context — skip (engineer / executive
                # have nothing to advise on without code or messages on screen).
                render_card(self.current_persona, ts, status="skipped",
                            skip_reason=f"no context for {self.current_persona}")
                return

            # Catch any variation of "nothing to say about the screen".
            # The model finds creative wordings despite the negative_prompts,
            # so we cast a wider net here rather than relying on exact phrases.
            _AC = (
                "all clear", "nothing to add", "nothing to comment",
                "nothing specific", "nothing noteworthy",
                "screen is empty", "screen appears empty", "screen appears to be",
                "screen looks empty", "screen is blank", "screen is clear",
                "nothing on screen", "nothing visible on",
                "no content", "no text", "no code", "no activity",
                "i don't see anything", "i cannot see", "i can't see",
                "doesn't appear to have", "does not appear to have",
            )
            all_clear = any(phrase in tip.lower() for phrase in _AC) or not tip.strip()

            # ── 2. Priority call ─────────────────────────────────
            priority    = self._priority_call(tip)
            final_score = (confidence + priority) / 2.0
            eff_thr     = self._effective_threshold(self.current_persona)

            # ── 3. Decide & emit ─────────────────────────────────
            if all_clear:
                render_card(self.current_persona, ts, status="suppressed",
                            apps=apps_seen, context_preview=preview,
                            confidence=confidence, priority=priority,
                            final_score=final_score, tip=tip,
                            threshold=eff_thr,
                            suppress_reason="model said 'all clear' — nothing to surface",
                            used_image=used_image)
                return

            is_auditor       = self.current_persona == "auditor"
            is_user_initiated = (
                self.current_persona in self.USER_INITIATED_PERSONAS
                or self._overridden_this_tick    # Cmd+1 / Cmd+`
            )
            # Per-persona dedup: same persona repeating itself is the noisy
            # pattern we want to suppress. Cross-persona overlap is fine.
            last_tip_here    = self._last_tip_by_persona.get(self.current_persona)
            is_exact_dup     = (last_tip_here is not None and tip == last_tip_here)
            above_thr        = final_score >= eff_thr
            should_notify = (
                is_auditor
                or (is_user_initiated and not is_exact_dup)
                or (above_thr and not is_exact_dup)
            )

            if should_notify:
                icon       = persona_data.get("icon", "👁")
                short_name = persona_data.get("title", f"The {self.current_persona.title()} Seer")
                title      = f"{icon} {short_name}"
                payload    = self.notify(title, tip, is_critical=is_auditor)
                if payload:
                    self.sent_history.append(tip)
                    self._last_tip_by_persona[self.current_persona] = tip
                    self.last_notified_persona = self.current_persona
                render_card(self.current_persona, ts, status="sent",
                            apps=apps_seen, context_preview=preview,
                            confidence=confidence, priority=priority,
                            final_score=final_score, tip=tip,
                            sent_payload=payload, used_image=used_image)
            else:
                # Honest suppression reason — the old code always printed
                # "final < threshold" even when the real cause was the dup guard.
                if is_exact_dup and above_thr:
                    reason = (f"{self.current_persona} just sent this same tip "
                              f"(final {final_score:.2f} ≥ {eff_thr:.2f}, but identical)")
                elif is_exact_dup:
                    reason = (f"{self.current_persona} just sent this same tip "
                              f"(final {final_score:.2f} also below threshold {eff_thr:.2f})")
                else:
                    reason = f"final {final_score:.2f} < threshold {eff_thr:.2f}"
                render_card(self.current_persona, ts, status="suppressed",
                            apps=apps_seen, context_preview=preview,
                            confidence=confidence, priority=priority,
                            final_score=final_score, tip=tip,
                            threshold=eff_thr,
                            suppress_reason=reason, used_image=used_image)

        except Exception as e:
            self.log(f"❌ Error during tick: {e}")


# ──────────────────────────────────────────────────────────────────
# Entrypoint
# ──────────────────────────────────────────────────────────────────

ENGINE_LOCK_FILE = "/tmp/theseer_engine.pid"   # singleton lock for theSeer.py


def _acquire_singleton_lock(path):
    """See notify_server.py for full docs. Returns open fd or None."""
    fd = open(path, "a+")
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (IOError, OSError):
        fd.close()
        return None
    fd.seek(0); fd.truncate()
    fd.write(str(os.getpid())); fd.flush()
    return fd


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TheSeer Logic Engine")
    parser.add_argument(
        "--test_mode", action="store_true",
        help="Bypass the MLX server and use simple local classifiers — confirms "
             "the full pipeline (screenpipe → persona → scoring → notification) "
             "without needing the AI stack.",
    )
    args = parser.parse_args()

    _lock = _acquire_singleton_lock(ENGINE_LOCK_FILE)
    if _lock is None:
        try:
            with open(ENGINE_LOCK_FILE) as _f:
                existing_pid = _f.read().strip() or "?"
        except Exception:
            existing_pid = "?"
        print(f"[theSeer] another instance is already running (PID {existing_pid}). "
              f"Exiting.\n  To stop the existing one:  kill {existing_pid}",
              file=sys.stderr, flush=True)
        sys.exit(1)

    seer = TheSeer(test_mode=args.test_mode)
    try:
        while True:
            seer.check_screen()
            time.sleep(cfg.CHECK_INTERVAL)
    except KeyboardInterrupt:
        print("\n👋 Exiting TheSeer Logic Engine...")
        sys.exit(0)
