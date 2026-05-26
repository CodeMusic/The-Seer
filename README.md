# TheSeer 👁

An ambient, screen-aware AI assistant for macOS. It quietly watches what's on your screen and pops a custom upper-right toaster when something's worth surfacing — a security exposure, a code suggestion, a writing nudge, a fact, or a quick joke when you ask for one. Click any toaster to open a chat window and ask follow-up questions. All inference runs **locally** on Apple Silicon. Nothing leaves your machine.

```
┌──────────────────────────────────────┐
│ TheSeer: Engineer                    │
│ 🛠 TheSeer                           │
│                                      │
│ Consider extracting this hard-coded  │
│ retry constant into a config value — │
│ you'll thank yourself when it changes│
│                                      │
│ click to chat ↗            more ▾    │
└──────────────────────────────────────┘
```

---

## The personas

TheSeer switches persona automatically based on what's on screen. Each persona has its own system prompt and behavior.

| Persona | When | What it does |
|---|---|---|
| 🚨 **Auditor** | A secret / key / token / password appears on screen | Brief, calm security warning naming the exposure and the recommended action. Sticky for a few ticks after the screen clears. |
| 🛠 **Engineer** | You're focused on an IDE (VS Code, Cursor, Xcode, JetBrains, …) | High-level architectural nudges or bug-spotting. No syntax nitpicking. |
| 💼 **Executive** | You're drafting in Mail, Slack, Teams, etc. | Steering advice on tone, clarity, professionalism. Doesn't write the message for you. |
| 👁 **Casual** | Default | One concrete, useful nugget per tick — a tool tip, a term meaning, a related idea. If the screen is genuinely quiet, fires a *lightweight ambient call* (a fact, a stretch reminder, a productivity nudge) so the loop never falls silent. |
| 🎭 **Entertainer** | You type "tell me a joke", "make me laugh", "entertain me", … | One brief, clever joke or quip. One-shot. |
| 📚 **Teacher** | You type "explain this", "teach me", "fun fact", "tldr", "in simple terms", "help me understand", … | One bite of trivia, a fact, or a clear analogy. One-shot. |
| 💪 **Motivator** | You type "motivate me", "inspire me", "give me a quote", "wise words", "pump me up", … | One short quote, piece of wisdom, or motivational line. One-shot. |
| 🌶️ **Sassy** | You type "be sassy", "sass me", "with attitude" — *or* hit **⌘+1** | Playful sarcasm / friendly snark about whatever's on screen. Affectionate, not mean. One-shot. |
| 🎤 **Performance** | You type "performance mode on", "be a poet", "rap about my screen" → enter. "performance mode off", "exit performance mode", "stop performing" → exit | **Sticky mode** (not one-shot). While active, every tick comes out as a rhyme, haiku, rap verse, or song snippet about what's on screen. |

Auditor always wins. The user-initiated personas (Entertainer / Teacher / Motivator / Sassy / Performance) bypass the normal score threshold — when you ask, you'll get something. The one-shots have a 5-minute cooldown so the trigger text on screen doesn't keep re-firing them. The rest are app-driven.

---

## How it decides whether to notify

Each tick (default every 15 s):

1. Pull the most recent OCR records from screenpipe.
2. Filter to only records newer than `RECENCY_WINDOW_SEC` (so old "phantom window" data doesn't trigger anything).
3. Classify the persona.
4. **Confidence call** to the local MLX server → produces a tip + a 0.0–1.0 score.
5. **Priority call** to the same MLX server with the tip + recent send history → produces a 0.0–1.0 score for how novel / worthwhile this is.
6. `final = (confidence + priority) / 2`.
7. If `final ≥ NOTIFICATION_THRESHOLD` **and** the *same persona* hasn't sent this exact text before → push the notification. Auditor and Entertainer/Teacher bypass the score check.
8. Either way, render a card-style entry to the terminal: 🔊 `sent`, 🔇 `suppressed`, ⏭ `skipped`, ⏸ `paused`, or ✅ `ready`.

Sample terminal output:

```
┌─ 14:23:01 · ENGINEER · 🔊 sent ─────────────────────────────────────
│  apps:    Cursor, iTerm
│  seen:    "[Cursor]: def retry(fn, max_attempts=3): for i in range..."
│  scores:  conf 0.80  ·  priority 0.65  ·  final 0.72
│
│  💬 Consider extracting that max_attempts=3 into a constant — easier
│     to tune later than hunting through the function body.
│
│  📨 TheSeer: Engineer
│       🛠 TheSeer
│       Consider extracting that max_attempts=3 into a constant —
│       easier to tune later than hunting through the function body.
└─────────────────────────────────────────────────────────────────────
```

---

## The Model: MiniCPM-V-4.6 (mxfp4)

TheSeer's brain is **MiniCPM-V-4.6**, a 4.6-billion-parameter open-source vision-language model from the MiniCPM team at OpenBMB / Tsinghua. The variant we ship — `mlx-community/MiniCPM-V-4.6-mxfp4` — has been converted to Apple's **MLX** format and quantized to **mxfp4** (a 4-bit floating-point representation). After quantization the entire model is small enough to load into ~3 GB of unified memory and runs at usable speed on Apple Silicon's GPU + Neural Engine.

### Why this matters for TheSeer

- **Fully local.** Every confidence call, priority call, ambient call, and chat reply runs on your machine. Your screen content — including credentials when the Auditor fires — never leaves the device.
- **Fast enough for an ambient loop.** Each tick fires two LLM calls (confidence + priority); on an M2 they complete in 1–3 seconds total, well within the 15-second tick interval.
- **No API keys, no rate limits, no surprise deprecations.** Open weights, run forever.
- **Vision-language.** MiniCPM-V is multimodal at the model level. We currently feed it OCR'd text only, but the architecture leaves room for future versions to send screenshots directly.

The quantization tradeoff: mxfp4 sacrifices some response quality vs. the full-precision model. That's part of why the **Confidence + Priority dual-LLM scoring** matters — it gives the system a way to filter out occasionally-bad outputs before they ever reach you.

Want better outputs at the cost of memory and speed? Swap `MODEL_ID` in `configuration.py` for `mlx-community/MiniCPM-V-4.6` (full precision) or a larger MLX-format VLM. Want it faster on lower-end Apple Silicon? Stick with mxfp4.

---

## Components

```
┌─────────────────────────────────────────────────────────────┐
│ theSeer.py        (foreground — the brain)                  │
│   • screenpipe HTTP poll                                    │
│   • persona classification                                  │
│   • confidence + priority LLM calls                         │
│   • per-persona dedup, score-based gate                     │
│   • renders card to terminal                                │
└─────────────────────────────────────────────────────────────┘
                            │
                  /tmp/theseer_notify (named pipe, one JSON / line)
                            │
┌─────────────────────────────────────────────────────────────┐
│ notify_server.py  (rumps menu-bar app, 👁 in tray)          │
│   • reads FIFO, draws custom NSPanel toasters via PyObjC    │
│   • hover → pointer cursor, hint says "click to chat ↗"     │
│   • click toaster body → opens chat window for that tip     │
│   • long bodies show "more ▾" → expand without opening chat │
│   • History submenu in tray, click any entry to re-open chat│
│   • while chat is open, touches /tmp/theseer_chat_active so │
│     theSeer.py pauses inference (no LLM contention)         │
└─────────────────────────────────────────────────────────────┘
                            │
                            ├─ screenpipe record  (port 3030, OCR)
                            └─ mlx_vlm.server     (port 8081, the model)
```

### Why custom toasters instead of `UNUserNotificationCenter`?

macOS 26 (Tahoe) silently demotes notifications from ad-hoc-signed apps to "Deliver Quietly" — they appear in Notification Center only, never as banners, regardless of what System Settings shows. We sidestep the entire notification subsystem by drawing our own borderless `NSPanel` via PyObjC. Tradeoffs:

- ✅ No notification permission, no Focus-mode silencing, no codesigning, no LaunchServices registration. Always works.
- ✅ Full UI control: hover effects, "more ▾" expand, click-to-chat hint, custom geometry.
- ❌ No Notification Center archival entry, no Lock Screen mirror, no Apple Watch mirror. (TheSeer's own History submenu fills the archival gap.)

---

## Requirements

- macOS 14+ (built and tested on macOS 26 / Tahoe)
- Apple Silicon (M1 / M2 / M3 / M4)
- Python 3.10+
- Node.js + `npx` (for screenpipe)
- ~9 GB free disk space (first-run MLX model download)

No Xcode, no codesigning, no `sudo`, no manual permission grants.

---

## Setup

```bash
./setup.sh
```

Installs Python deps (`rumps`, `requests`, `mlx-vlm`) and primes the `screenpipe` cache. Then:

1. **Grab a screenpipe token** (one-time):
   ```bash
   npx screenpipe@0.3.345 auth token
   ```
   Paste the value into `SCREENPIPE_TOKEN` in `configuration.py`.

2. **Launch:**
   ```bash
   ./start_theSeer.sh              # full stack (uses MLX)
   ./start_theSeer.sh --test_mode  # smoke-test pipeline without MLX
   ```

   First MLX launch will download the model (~9 GB) — subsequent launches are fast.

3. **Verify:** you should see
   - `👁` in your menu bar (may be hidden by a wide active-app menu — switch to Finder to confirm)
   - A "TheSeer: Online 👁" toaster pop in the upper-right
   - Card output in the terminal every ~15 seconds

`Ctrl+C` cleanly shuts down screenpipe, the MLX server, and the notification daemon.

---

## Using it

- **Notice the toaster** in the upper-right — auto-dismisses after ~9 s.
- **Hover** to see the "click to chat ↗" hint brighten (cursor changes to pointer).
- **Click** anywhere on the body → centered chat window opens, pre-loaded with the tip. Type follow-up questions; press Enter or click Send.
- **Long body?** A small **more ▾** button appears in the corner. Click it to expand the toaster (clicking it does *not* open chat). Click **less ▴** to collapse.
- **Menu-bar 👁** → **History** submenu lists the last 20 notifications. Click any entry to reopen its chat.
- **Type "tell me a joke"** anywhere on screen → Entertainer fires for one tick.
- **Type "explain this"** or "tldr" or "in simple terms" → Teacher fires for one tick.
- **Close the chat** → TheSeer resumes screen-watching automatically.

### Keyboard shortcuts

Two global shortcuts work anywhere on the system:

| Shortcut | What it does |
|---|---|
| **⌘ + 0** | Replays one toaster *per persona* that has any history — the most recent message from each, stacked oldest-bottom. **Idempotent**: dismisses any toasters already on screen first, so mashing it never accumulates. The startup "TheSeer: Online" ping is excluded from the replay pool. |
| **⌘ + 9** | **Toggles** the chat window for the most recent notification — opens if closed, closes if open. Press it again to undo an accidental press. |
| **⌘ + 1** | Secretly nudges the next tick into 🌶️ **Sassy Seer**. Useful when showing TheSeer off — press it before the next tick lands and you'll get a snark-flavoured reply, then automatic revert. |
| **⌘ + 2** | Toggles 🎤 **Performance mode** on / off (same effect as typing "performance mode on" / "performance mode off"). |
| **⌘ + \`** | Wildcard — picks a *random* persona for the next tick (Casual / Engineer / Executive / Entertainer / Teacher / Motivator / Sassy). After that one tick, normal persona-detection resumes. |

These rely on a global key listener (`pynput`) which **requires Accessibility permission** the first time you run TheSeer:

1. The first time you launch, macOS will prompt: *"python3 wants access to control your computer"*. Click **Open System Settings**.
2. Toggle on the entry for `python3` (or whichever Python `which python3` points to).
3. Quit and re-launch TheSeer.

You can verify it works by pressing **⌘+9** with no chat open — the most recent toaster's chat window should appear. If nothing happens, see the troubleshooting note below.

---

## Files

| File | Role |
|---|---|
| `theSeer.py` | Main loop. Polls screenpipe, classifies persona, calls MLX twice (confidence + priority), gates with per-persona dedup + threshold, writes notification JSON to the FIFO, renders terminal cards. |
| `notify_server.py` | rumps menu-bar app. Reads FIFO, draws PyObjC toasters with hover/click/expand, manages the chat window, maintains the History submenu. |
| `personas.py` | The persona dictionary: prompts, triggers, examples. |
| `configuration.py` | Tunable knobs (URLs, intervals, thresholds, cooldowns). |
| `start_theSeer.sh` | Launcher — boots screenpipe + MLX + notify_server, then runs theSeer.py in the foreground. Traps Ctrl-C for clean shutdown. |
| `setup.sh` | One-shot installer: `pip install -r requirements.txt` + screenpipe prefetch. |
| `requirements.txt` | `rumps`, `requests`, `mlx-vlm`. |

---

## Tuning

### `configuration.py`

| Key | Default | Effect |
|---|---|---|
| `CHECK_INTERVAL` | `15` | Seconds between screen checks. |
| `CONTEXT_LIMIT` | `10` | How many recent OCR records to ask screenpipe for. |
| `MAX_TOKENS` | `60` | Max tokens for the confidence reply. |
| `REVERT_THRESHOLD` | `2` | Consecutive clean ticks before the Auditor stops being sticky. |
| `NOTIFICATION_THRESHOLD` | `0.6` | Minimum *final* score required to push a notification. |
| `RECENCY_WINDOW_SEC` | `20` | Drop screenpipe records older than this. Raise if you see lots of "no OCR" skips on an idle screen; lower if a window you switched away from still triggers tips. |
| `ENTERTAINER_COOLDOWN_SEC` | `300` | Minimum seconds between Entertainer firings. |
| `TEACHER_COOLDOWN_SEC` | `300` | Same, for Teacher. |

### `personas.py`

Each persona has:

- `prompt` — its system prompt (with few-shot examples).
- `negative_prompt` — what to avoid.
- `trigger_apps` or `trigger_keywords` — what activates it.
- `exclude_apps` — apps whose OCR is dropped before the prompt is built (e.g. Casual ignores Terminal noise).

Add an app to a persona's `trigger_apps` if TheSeer keeps misclassifying. Add a keyword to `auditor`'s `trigger_keywords` if a new secret format slips by.

### `notify_server.py` (top of file)

| Key | Default | Effect |
|---|---|---|
| `PANEL_WIDTH` / `PANEL_HEIGHT` | `360` / `100` | Toaster size. |
| `DISMISS_AFTER_SEC` | `9.0` | How long a toaster lingers. |
| `SOUND_NAME` | `"Frog"` | macOS system sound (or `None` to mute). Try `Pop`, `Purr`, `Tink`, `Glass`. |
| `SOUND_VOLUME` | `0.3` | 0.0–1.0. |
| `HISTORY_LIMIT` | `20` | Max entries in the menu-bar History submenu. |

---

## Troubleshooting

**No toaster appears at all.**
Is `notify_server.py` still alive? `pgrep -af notify_server`. If it crashed, re-run `./start_theSeer.sh`. Check `python3 -c "import rumps, AppKit"` works — if not, `pip install rumps` in the right Python.

**The `👁` icon isn't in the menu bar.**
macOS 26 silently clips menu-bar items when the active app's menu titles run too wide. Switch to Finder (very short menu) — the eye should appear. Long-term fix: free up some menu-bar real estate, or use a tool like Bartender.

**Chat input ignores my keystrokes (typing goes elsewhere).**
Fixed: TheSeer temporarily promotes itself to a regular app while the chat is open. If you still see this, make sure you're on the latest `notify_server.py` (looks for `setActivationPolicy_` in `_build_window`).

**`screenpipe failed to respond on port 3030`.**
Usually a corrupted npm cache. Clear and reinstall:
```bash
rm -rf ~/.npm/_npx
./setup.sh
```

**MLX server fails to load.**
First run downloads ~9 GB. Check `mlx_server.log` for progress. Subsequent launches are fast.

**Lots of `⏭ no OCR within last Ns` skip cards.**
The screen is genuinely idle (good!) OR `RECENCY_WINDOW_SEC` is set too low for `CHECK_INTERVAL`. Keep `RECENCY_WINDOW_SEC ≥ CHECK_INTERVAL + 5`.

**Wrong persona is selected.**
Edit `personas.py` — add the app name (exactly as screenpipe reports it, visible in the `apps:` line of each terminal card) to the relevant `trigger_apps` list.

**Same tip keeps repeating.**
Per-persona dedup should catch exact repeats. If you're seeing *semantic* repeats (different words, same meaning), the priority LLM should give them a low score and they'll drop below the threshold — try lowering `NOTIFICATION_THRESHOLD` only if you're confident; raising it suppresses more.

**Cmd+0 / Cmd+9 don't do anything.**
The pynput listener is running but macOS is silently dropping its events because Accessibility permission hasn't been granted yet.
1. Open *System Settings → Privacy & Security → Accessibility*.
2. Find the `python3` entry — the path should match `which python3` in your shell.
3. Toggle it on. If it isn't in the list, click **+**, navigate to your Python (often `/Users/<you>/.pyenv/versions/<x.y.z>/bin/python3`), and add it.
4. Quit `./start_theSeer.sh` (Ctrl+C), re-launch — hotkeys should fire on next run.
If you still see no effect, check `pip show pynput` — if missing, run `pip install pynput`.

---

## Roadmap

- Right-click toaster context menu (history + quit), so the menu-bar overflow problem becomes a non-issue.
- Settings UI + `.env`-based config (move config out of `configuration.py`, add a settings window, save-and-restart flow).
- Optional: package as a real `.app` so the tray icon survives independently of the terminal.
- Per-persona toaster colour accent.
