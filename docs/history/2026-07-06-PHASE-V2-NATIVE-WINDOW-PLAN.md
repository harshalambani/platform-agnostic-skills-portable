# Phase v2 — Native window (one-click quit) — Design / Plan

Tracking issue: **#40** — "wrap the app in a native window (pywebview) for
intuitive one-click quit". Ships alongside #43 (v2 icon refresh).

Status: decisions locked; **Phase A (source prototype) + Phase B (frozen build)
complete & verified 2026-07-06**. Phase C (UX/branding) + Phase D (fallback/CI)
next.

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
  ✅ **Done in Phase B** — both ship entry-point PyInstaller hooks that are
  auto-discovered; the only spec change needed was adding `webview` + `clr` to
  `hiddenimports` (lazy imports) + `collect_submodules("webview")`.
- Bundle `WebView2Loader.dll` and the `Microsoft.Web.WebView2.*` managed assembly.
  ✅ **Verified bundled** — the hooks carry them plus `WebBrowserInterop.*` and
  the pythonnet runtime; no manual DLL vendoring required.
- Expect a **bundle-size increase**; `--onedir` already, so no onefile extraction
  cost.
- **De-risk early:** the frozen build is the riskiest part — prototype the frozen
  build in Phase B before polishing UX. ✅ **Done — risk cleared.**

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

- **A — Source prototype:** ✅ **DONE (2026-07-06).** server-thread + pywebview
  window in dev mode; verified window-close → clean process exit with the atexit
  API-key wipe firing (harness: build_app in window mode → auto-close window →
  exit 0 + atexit sentinel present). Implemented in `ui/webui.py`:
  `_resolve_launch_mode()` (headless / browser / window), `_native_window_available()`
  + `_webview2_runtime_present()` (WebView2 registry pre-check), `_run_native_window()`
  (Gradio launched `prevent_thread_lock=True`, `webview.start()` on the main thread,
  `_terminate_process()` on close), silent browser fallback, Exit button gated off in
  window mode, `--no-window` / `PA_SKILLS_NO_WINDOW` escape hatch. pywebview/pythonnet
  are **soft deps** (lazy, feature-detected — the app falls back to browser if absent);
  declaring them in `requirements.txt` + frozen hooks is Phase B. Known cosmetic:
  benign asyncio `WinError 10054` teardown traces on close (invisible in the frozen
  `console=False` build) — tidy in Phase C. No packaging yet.
- **B — Frozen build:** ✅ **DONE (2026-07-06).** `pywebview` + `pythonnet`
  added to `requirements.txt` + `pyproject.toml`; `requirements-lock.txt`
  regenerated (uv, hashed — pulls in `clr-loader`, `cffi`). `paskills.spec`:
  `webview` + `clr` added to `hiddenimports` (the app imports them lazily, so
  static analysis misses them — this triggers their bundled PyInstaller hooks),
  plus `collect_submodules("webview")` so the runtime-selected backend
  (`webview.platforms.winforms`/`edgechromium`) resolves. Verified against the
  onedir exe: the WebView2 managed DLLs (`Microsoft.Web.WebView2.*`,
  `WebView2Loader.dll` x64/x86/arm64, `WebBrowserInterop.*`) and pythonnet's
  `Python.Runtime.dll` + `clr_loader/ClrLoader.dll` are bundled;
  default launch opens a **real WebView2 window** (confirmed via spawned
  `msedgewebview2.exe` children — pythonnet/clr loads fine when frozen);
  closing the window (WM_CLOSE) exits the process **cleanly, exit code 0, no
  orphan `pa_skills.exe`**, and the WebView2 children self-reap to baseline
  within ~2s (no leak); headless (`PA_SKILLS_NO_BROWSER=1`) still serves HTTP
  200 with the window OFF (CI compat-check path intact). The only hook warning
  is a benign `webview.platforms.android` (needs an `android` module — n/a on
  Windows).
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
