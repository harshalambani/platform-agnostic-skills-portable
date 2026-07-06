# Phase v2 — Native window (one-click quit) — Design / Plan

Tracking issue: **#40** — "wrap the app in a native window (pywebview) for
intuitive one-click quit". Ships alongside #43 (v2 icon refresh).

Status: **design — awaiting approval before code.**

---

## 1. Problem

The frozen app is a **windowless Gradio server** (`console=False`, `--onedir`).
On launch it binds `127.0.0.1:<free-port>`, writes `ui_port.json`, and opens the
user's **default browser** (`app.launch(inbrowser=True)`, `ui/webui.py:441`).

There is **no OS window that owns the process**, which forces a clumsy
two-step quit:

1. Click the header **Exit** button (arms a 0.8 s shutdown timer,
   `_on_exit_click`, `ui/webui.py:226`).
2. Manually close the browser tab.

Closing the tab alone is unreliable: `Blocks.unload` arms a 3 s grace timer
(`_schedule_shutdown`, `ui/webui.py:215`) that a reload/navigation cancels, and
with multiple tabs open only the Exit button reliably stops the server. A
lingering `pa_skills.exe` makes the PortableApps Launcher (`WaitForProgram=true`)
report **"did not close correctly"** and blocks upgrades.

**Goal:** closing one window quits the app — no Exit button, no orphan process.

---

## 2. Current mechanics (what we must preserve)

- `app.launch(...)` at `ui/webui.py:441` is **blocking** — the main thread sits
  in Gradio's server loop. No `prevent_thread_lock`.
- Clean exit runs through `_terminate_process()` (`ui/webui.py:190`):
  `atexit._run_exitfuncs()` **then** `os._exit(0)`. The atexit pass wipes the
  `%TEMP%` legacy-config dirs that hold **decrypted API keys** — this MUST keep
  running on every exit path.
- `ui_port.json` is consumed by the PA Launcher and the CI smoke test.
- CI **compat-check** smoke test (`.github/workflows/compat-check.yml`) launches
  the exe **headless** with `PA_SKILLS_NO_BROWSER=1`, waits for the port file,
  and does an HTTP `GET` — it must NOT try to open a GUI window on the runner.

---

## 3. Proposed approach — pywebview + WebView2

Host the existing Gradio URL inside a native window via **pywebview**
(EdgeChromium / WebView2 backend on Windows).

### Threading model
Both `webview.start()` and `app.launch()` want to block the main thread, and the
Windows GUI message pump must run on the main thread. So:

1. Launch Gradio **non-blocking** on a background thread:
   `app.launch(..., prevent_thread_lock=True)`.
2. Read the resolved local URL (`http://127.0.0.1:<port>`).
3. On the **main thread**: `webview.create_window(APP_TITLE, url, ...)` then
   `webview.start()`.
4. `webview.start()` returns when the window closes → call
   `_terminate_process()` (atexit cleanup + `os._exit(0)`).

### Window lifecycle
- Title = `APP_TITLE`; window icon = `bundling/icons/appicon.ico` (**ties to
  #43** — the new artwork shows in the title bar / taskbar).
- Close = quit. No confirm dialog. Route through `_terminate_process()` so the
  API-key temp wipe always fires.
- Optional (v2.1): persist window size/position.

### Fallback / degrade path (important)
pywebview on Windows needs the **WebView2 runtime**. It ships with Windows 11 /
Evergreen Edge (present by default on Win11), but may be absent on some Win10
machines. If pywebview import or `create_window` fails, **fall back to today's
browser-open + Exit-button behavior** — never hard-fail the launch.

### Mode gating (env flags)
- `PA_SKILLS_NO_BROWSER=1` → **no window, no browser** (headless). Keep the
  server alive as today so the CI smoke test's HTTP GET works. This is the CI
  path — the native window must be OFF here.
- New `PA_SKILLS_NO_WINDOW=1` → browser mode even outside CI (debug escape hatch).
- Default (frozen, interactive) → native window.

---

## 4. Packaging impact (PyInstaller)

- Add deps: `pywebview`, `pythonnet` (+ `clr_loader`). pywebview ships
  PyInstaller hooks; pythonnet/clr needs its loader bundled.
- Bundle `WebView2Loader.dll` and the `Microsoft.Web.WebView2.*` managed assembly.
- Expect a **bundle-size increase**; `--onedir` already, so no onefile extraction
  cost.
- **De-risk early:** the frozen build is the riskiest part — prototype the frozen
  build in Phase B before polishing UX.

---

## 5. Alternatives considered

| Option | Verdict |
|---|---|
| **pywebview + WebView2** | **Recommended** — mature, tiny API, native WebView2 on Win, PyInstaller hooks exist. |
| Edge/Chrome `--app=<url>` chromeless window | Lighter (no new deps) but close-detection is process-handle hacky and depends on a specific browser being installed. |
| Electron / Tauri | Overkill — new toolchain, large runtime, rewrites the shell. |
| Raw win32 + WebView2 SDK | Most control, most code to own. |
| Keep browser, auto-close via WS heartbeat | Doesn't remove the two-step; still unreliable with multiple tabs. |

---

## 6. Risks

- **WebView2 runtime absence** (rare on Win11, possible Win10) → mitigated by the
  browser fallback; optionally detect + point to the MS bootstrapper.
- **pythonnet/clr under PyInstaller** can be finicky → prototype frozen build first.
- Larger download.
- **Not** a code-signing fix — the unsigned exe still trips SmartScreen; that's
  #38, independent of this work.

---

## 7. Phased plan

- **A — Source prototype:** server-thread + pywebview window in dev mode; verify
  window-close → clean process exit (atexit runs). No packaging yet.
- **B — Frozen build:** spec/hooks for pywebview + pythonnet; build + run the
  onedir exe with a real window. Highest-risk step — do it early.
- **C — UX + branding:** window title + `appicon.ico` (#43); demote/remove the
  Exit button (keep it only in browser-fallback mode); update PA Launcher notes.
- **D — Fallback + CI:** implement the env-flag gating; keep the compat-check
  smoke test on the headless (`PA_SKILLS_NO_BROWSER`) path; add a window-mode
  manual test to the release checklist.

---

## 8. Decisions (locked 2026-07-06)

1. **Shell:** pywebview + WebView2. (Not the Edge `--app` hack.)
2. **WebView2 absent:** silently fall back to browser + Exit-button behavior. No
   install prompt.
3. **Exit button:** removed in native-window mode; still shown in the
   browser-fallback path where it's the only quit affordance.
