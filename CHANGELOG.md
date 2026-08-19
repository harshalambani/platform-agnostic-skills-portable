# Changelog

All notable changes to this project are recorded here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> **Note on release tags:** on 2026-07-31 every tag below `v2.23.1` was deleted
> to remove the publicly downloadable source archives GitHub generates per tag.
> The commits all remain on `main`, so the history behind older entries is
> intact, but the version links no longer resolve to a tag.

## [Unreleased]

### Added
- **New ITR Foreign Income Pack skill parses a foreign broker's consolidated
  tax report and populates Schedule FA, Schedule FSI, Form 67, and Schedule
  TR** (#195, `src/agents/skill_itr_workbook/scripts/parse_foreign.py`). The
  input is optional (`foreign_report_xlsx`) so a run with no foreign report
  supplied is unaffected. The report mixes two different reporting years on
  the same workbook - Schedule FA reports foreign holdings as of the
  CALENDAR year, while the Dividend and Interest sheets report the FINANCIAL
  year - and the parser deliberately leaves that mismatch alone rather than
  reconciling the two, since reconciling them would misrepresent what each
  schedule is actually reporting. Every numeric cell is read through one
  shared parser that never turns a non-numeric cell (blank, "-", or
  instructional prose) into `0.0`; instead it returns the value alongside a
  flag naming why it could not be parsed, because a zero on a tax return is
  an assertion of fact, not a safe default. A required sheet missing
  entirely raises and stops the run, but a header that cannot be found
  within a present sheet only marks that one schedule unparsed and warns,
  so a layout change in one sheet does not take down every other sheet in
  the same report.
- **Foreign broker dividends are now wired into Schedule OS and the 234C
  advance-tax instalment buckets** (#196,
  `src/agents/skill_itr_workbook/scripts/schedules.py`,
  `src/agents/skill_itr_workbook/scripts/write_workbook.py`). PR #195 parsed
  the broker's Dividend sheet but nothing downstream consumed it; this PR
  surfaces those figures on their own new Other Sources rows, clearly
  labelled and kept strictly separate from the book-derived Other Sources
  total, because the GnuCash book may already tag the same receipts under
  `OS_DIVIDEND` and summing both would double-count. Ordinary dividend
  income and deemed dividend under s.2(22)(f) are tracked as separate
  series throughout. Each vendor quarterly period is assigned to one of the
  five statutory 234C windows by parsing the period label's END date (e.g.
  "16-Jun to 15-Sep" -> 15-Sep) and resolving it through the shared
  bucket-index helper in `quarters.py`, rather than by the period's position in
  the list, so there remains exactly one definition of the 234C windows in
  the codebase. A period whose label cannot be parsed leaves its window
  unfilled and warns instead of guessing; two periods that resolve to the
  same window are summed with a warning instead of one silently overwriting
  the other.
- **Form 16 parsing now extracts the taxpayer's old/new tax-regime election
  and resolves which regime a workbook build actually uses** (#194,
  `src/agents/skill_itr_workbook/scripts/parse_form16.py`,
  `src/agents/skill_itr_workbook/agent.py`). `Form16Data` gains `regime`,
  read from the Part B s.115BAC(1A) opt-out field ("opted out?" Yes means
  the old regime, No means the new regime); when that field cannot be read,
  `regime` is left unset and `regime_unparsed_reason` records why, rather
  than guessing, since old and new regime produce materially different tax.
  The workbook build resolves the regime to use through a priority chain in
  the new `_resolve_regime()`: an explicit `regime_override` always wins if
  one is given, then the regime parsed from Form 16, then the entity's
  configured default. When an explicit override disagrees with what Form 16
  says, or when neither an override nor a parsed Form 16 regime is
  available and the entity's configured default is used unconfirmed, a
  warning is added to the run summary - the build still completes and the
  workbook is still written either way.

### Changed
- **`pypdfium2` bumped from 5.12.1 to 5.13.0** in the pdf-ocr dependency
  group (#185).
- **Seven further dependency bumps, all `requirements-lock.txt`-only** (no
  source changes):
  - `gradio` 6.22.0 -> 6.24.0, ui group (#186)
  - langchain-ecosystem group, 5 updates (#184): `langchain` 1.3.14 ->
    1.3.15, `langchain-core` 1.5.3 -> 1.5.4, `langchain-openai` 1.4.1 ->
    1.5.0, `langgraph` 1.2.10 -> 1.2.11, `langgraph-checkpoint` 4.1.1 ->
    4.2.0
  - `xxhash` 3.8.1 -> 4.0.0 (#190) - a MAJOR version bump
  - `huggingface-hub` 1.26.1 -> 1.27.0 (#187)
  - `greenlet` 3.5.4 -> 3.5.5 (#188)
  - `charset-normalizer` 3.4.9 -> 3.5.0 (#191)
  - `filelock` 3.32.2 -> 3.32.3 (#192)

## [3.6.0] — 2026-08-08

This release makes the launcher splash disappear when the app window is
actually ready instead of when a timer runs out, which removes the need for
the splash duration to double as a guess at how long startup takes on a given
machine.

### Added
- **The PortableApps.com Launcher splash now dismisses itself the moment the
  app's window is actually ready**, instead of running out a fixed timer
  (`ui/_splash.py`). The splash is a separate window owned by the launcher
  process, not this app, and it takes a duration with nothing telling it the
  app has finished starting — so the old `SplashTime` had to double as a
  guess at startup time, and a guess calibrated on one machine is wrong on
  every machine with different hardware: too low and it clears into a blank
  desktop, too high and a topmost splash sits on top of an already-usable
  window. `PostMessage(WM_CLOSE)` does nothing to that window, but it turns
  out the underlying NSIS splash plugin is launched without `/NOCANCEL`,
  whose documented default is "exit on click" — so this module finds the
  splash window (class `_sp`, owned by `PASkillsPortable.exe`) and
  synthesizes the click, wired to pywebview's `window.events.shown` in
  `ui/webui.py`. Everything about this is best-effort and self-contained: it
  is gated to Windows, wrapped so no exception can ever escape it, and caps
  its own search at a couple of seconds, so if the splash window can't be
  found (source mode, splash disabled, launched without PAL, or a future PAL
  version changes the class name or adds `/NOCANCEL`) it silently no-ops and
  the splash simply runs out its timer as it always did.

### Changed
- **`SplashTime` raised from 11000 to 45000** (`bundling/templates/
  PASkillsPortable.ini.tmpl`). With the splash now dismissed on window-ready
  rather than timed out, this value stopped being an estimate to land close
  to and became a maximum: it only needs to cover the slowest cold start
  ever measured (44s) without penalizing anything faster, which the old
  fixed-timer design explicitly gave up on because covering the cold-start
  case would have meant overshooting every warm start instead. The
  surrounding comment block was rewritten from scratch — it also corrects an
  assumption baked into every prior tuning of this value, that the splash
  appears at launcher start (t=0); it actually appears about 1.3s in, so
  every earlier number was implicitly more generous than it looked.

## [3.5.1] — 2026-08-08

A security release. Nothing in it changes what the app does; three separate
paths were leaking, or could leak, a live credential to somewhere it had no
business being.

### Security
- **Statement passwords are no longer printed to stdout** (#165). Every
  credit-card sort run echoed the working PDF password to the console, which
  lands in logs, scrollback, and anything the operator pastes when asking for
  help. The run summary now reports the password's *position* in the supplied
  list (`password #2`) instead of its value — unambiguous to whoever holds the
  list, worthless to anyone else, and strictly more useful than the value for
  identifying which entry to fix. One instance was subtler than the rest and
  was found by reading the code rather than from any alert: the code printed a
  filename stem, and in the single-password format the stem *is* the password.
  A source-level regression test now fails if any of the removed expressions
  reappear, because copy-paste is how this class of bug comes back.
- **Statement passwords no longer appear on the qpdf command line** (#167).
  They were passed as a `--password=...` argument, and on Windows any other
  local, unprivileged process can read another process's command line out of
  the process table for as long as it runs. The password is now written to
  qpdf's stdin via `--password-file=-`, so it never enters argv; an empty
  password passes no password option at all, which is qpdf's default anyway.
  Verified against the vendored qpdf 11.9.1 itself rather than its
  documentation — correct password, wrong password, unencrypted file, and
  direct argv inspection.
- **The mapping-rules temp file is created with `mkstemp`** (#166), so it is
  opened with an exclusive, unpredictable name instead of a guessable path in
  a world-writable directory. The CI workflow token is also now scoped
  explicitly rather than inheriting the default permission set.

### Fixed
- **CI fails the job when tests fail** (#163). A `continue-on-error` left over
  from an earlier debugging pass meant a red suite still produced a green
  check. This is the guard that should have caught the earlier
  green-over-zero-tests episode, and it was itself disabled.

### Changed
- **Splash timer drops from 13s to 11s.** It was sized against a 14.6s warm
  frozen start, but the startup work since brought warm start to a measured
  11.77s, so 13s had quietly become an overshoot. The splash is a timed,
  topmost overlay that nothing tells the app is ready, so overshooting parks
  it on top of a window the user could already be using — undershooting just
  ends it a beat early — which is why the value is set under the measured
  figure rather than over it. Not re-measured on 3.5.0's dependency set; that
  only errs in the tolerated direction.

## [3.5.0] — 2026-08-07

### Security
- **Clears all four open dependency advisories:** cryptography 50.0.0 (1
  high) and aiohttp 3.14.3 (1 high, 2 moderate). Because
  `bundling/build.py` installs the lock with `--no-deps`, those vulnerable
  versions were genuinely present in shipped artifacts, not merely named in
  a resolution nobody used.

### Fixed
- **CI now installs `requirements-lock.txt` instead of the loose
  `requirements.txt` pins** (#161), so the versions the test suite exercises
  are the versions that ship. Every previous release tested a different
  version set than it shipped. Adds `scripts/ci_lock_subset.py`, which drops
  the native-window pair (`pywebview`, `pythonnet`) block-aware rather than
  with grep, and refuses both to drop a package a kept one requires and to
  accept an exclude name absent from the lock.

### Changed
- **22 packages moved in one resolved set** (#162), superseding ten
  single-package dependabot PRs; 128 packages before and after, none added,
  none removed. Notables: gradio 6.21.0 -> 6.22.0, openai 2.46.0 -> 2.53.0,
  cryptography 49.0.0 -> 50.0.0, fastapi 0.139.0 -> 0.141.1, pandas 3.0.3 ->
  3.0.5, starlette 1.3.1 -> 1.4.1, aiohttp 3.14.1 -> 3.14.3. `websockets`
  deliberately holds at 15.0.1 because `langgraph-sdk` 0.4.2 caps it below
  16; `tomlkit` 0.14.0 and `click` 8.4.2 were re-picked unchanged by a fresh
  resolve, confirming the #150 hand-pins were right.
- **A failing test now fails CI** (#163). The exit-1 tolerance was written
  with its own removal condition attached ("once the suite is green on
  CI"); #161 met it.

## [3.4.1] — 2026-08-06

### Fixed
- **Closing the app no longer blocks relaunching it for ten seconds.** The
  window vanished on the first click, but the process lived on for a measured
  **10.4s warm / 17.1s cold** afterwards, and the PortableApps launcher holds
  its single-instance mutex for that whole time. Launching again inside it did
  nothing whatsoever — no window, no error, no message (the launcher's
  `SinglePortableAppInstance` guard quits silently by design) — which is
  indistinguishable from the app being broken.

  The fix is in two halves, and it only works as a pair — the first half on its
  own was measured making things **worse** (22.8s warm).

  1. *Exit on the window's `closing` event, not on `webview.start()` returning.*
     `closing` is a locking pywebview event, so the handler runs synchronously on
     the GUI thread the moment the close is requested — measured 65ms after
     `WM_CLOSE` — rather than after the whole WebView2 unwind.
  2. *Stop calling `atexit._run_exitfuncs()` on that path.* This is what made
     half 1 backfire. The registered set is not only ours: it includes
     `concurrent.futures.thread._python_exit`, which joins every
     ThreadPoolExecutor worker, and with Gradio's server threads still up it does
     not come back — **18.4s** of it on a real close, all of it with the window
     already off screen and the mutex still held. The two cleanups that genuinely
     have to run (they wipe the %TEMP% legacy-config dirs holding decrypted API
     keys, and the download staging dir) now go through a small registry in
     `ui/_config.py`, so the close path runs exactly those and then exits. A
     normal interpreter shutdown still runs them the ordinary way.

  Measured on the built app, from `WM_CLOSE` to the launcher releasing its
  mutex: **23.43s → 2.82s** (2.20s of that the app, 0.62s the launcher's own
  cleanup, cold and warm alike). Source mode, close to process exit: 18.39s →
  2.39s.

  (An earlier draft of this entry said the registered atexit handlers "total
  0.000s". That probe wrapped `atexit.register` ahead of the heavy imports and so
  never saw the handler that mattered; the 18.4s above is from a real close.)

- **The Home status panel and the Model dropdowns no longer reflow when their
  real values arrive.** The deferred startup probes fill both anywhere from ~0.1s
  to ~2s after the window appears. Until then the status panel was a single
  italic line that grew into a heading plus three bullets *per endpoint*, shoving
  the page down just as the eye landed on it; and every Model dropdown showed the
  *configured default* drawn exactly like a confirmed, probed entry, which then
  silently swapped for the real one.

  Both now start in the shape they finish in. The status panel emits the real
  block structure from config alone — no socket — with a ⚪ dot and a "Checking…"
  detail line, so only the dot and that line change on fill and nothing moves.
  The dropdown seeds one "Loading models…" choice whose **value** is still the
  configured default: a Run fired inside that window submits exactly what it
  always did, only the label stops claiming to be settled. Deliberately not
  spinners — on a 0.1s fill a spinner announces a wait that is already over.

- **The splash was sized against the wrong measurement and cleared while the
  screen was still empty.** It was set to 6s from a ~9s *source-mode* start; the
  frozen exe actually takes **14.6s warm** to show a window, leaving ~8.6s of
  nothing after the splash had gone — which is precisely the "is it starting?"
  gap the splash was added to fill. Now 13s, measured on the frozen build.
  Cold starts (44.0s) are still not fully covered, deliberately: stretching that
  far would park a topmost splash over a usable window on every warm start.

- **The splash showed the previous release's version number.** 3.4.0 shipped one
  reading "Version 3.3.0". The build renders it with Pillow, which is a
  dependency of the *app*, not of whatever interpreter runs `build.py` — on CI
  that is a bare `setup-python` with no packages, so the render raised
  `ModuleNotFoundError` and the fallback shipped the committed image. It is now
  rendered by the build venv's interpreter, and the fallback says out loud that
  its version line may be wrong.

### Changed
- **Reworded the splash subtitle and the package description.** The splash said
  "accounting and tax skills - offline". "Offline" was simply untrue — the app
  works against any OpenAI-compatible endpoint, cloud included, as its own Home
  tab says. The splash now makes the durable claim ("Platform Agnostic Skills -
  LLM powered", which also expands the acronym for a first-time user and stays
  true as the skill set broadens), while `appinfo.ini`'s description carries the
  specific, current one — accounting and tax — and is the single line to broaden
  when that stops being what is in the box.

## [3.4.0] — 2026-08-06

### Changed
- **The app starts in about half the time — measured 19.4s to 8.9s** (warm,
  source mode, launch to a constructed UI). Almost all of the saving is one
  thing: the LLM endpoint probe no longer runs on the startup path.

  It used to run there five or more times over. The Home tab probed *every*
  configured endpoint to draw its status dots, and each skill tab probed the
  active one again for its Model dropdown — all synchronous, all before a
  window existed, and an endpoint that is merely switched off pays a full
  socket timeout rather than failing fast. That was roughly 11 of the 19
  seconds, spent on sockets, with nothing on screen.

  None of it is needed to *construct* the UI, only to fill it in. The probe now
  runs once on a background thread started **before** `import gradio` — the
  single biggest remaining cost at ~6.5s and not something that can be avoided
  — so the network wait hides inside an import already being paid for. Home and
  the Model dropdowns then read that one shared result through a load event
  when the browser connects: both fills now measure at or under 0.05s, because
  the probe has already finished. **"Refresh status" still means refresh** — it
  ignores the cache and goes to the wire, and updates the shared result for
  everyone else on the way back.

- **The PortableApps launcher now shows a splash while the app starts.** Until
  the Gradio server is up there is nothing to look at, which reads as "did it
  launch?" — the same problem GnuCash Portable solves the same way. The splash
  carries the version and is redrawn per build, since the launcher paints the
  image and nothing else, so anything the user is meant to read has to be in
  it. Its duration is deliberately set *under* the expected start: it is a
  timed, topmost overlay with no way to know the app is ready, so overshooting
  would park it on top of a usable window.

## [3.3.0] — 2026-08-05

### Added
- **An entity can now record *why* the 31 October due date applies to it.**
  `audit_case: true` has always meant "the s.139(1) due date is 31 October",
  but it is *named* for only one of the routes to that date — own liability to
  audit u/s 44AB. s.139(1) Explanation 2 gives the same date to a working
  partner of a firm whose accounts are audited, someone who is not an audit
  case himself, and the workbook was printing the wrong statutory reason for
  that filer. A new optional `audit_case_basis` (`self_44ab` or
  `partner_of_audited_firm`) records the route, and the workbook spells it out
  beside the due date: *"Due date for furnishing the return — working partner
  of a firm whose accounts are liable to audit (s.139(1) Expl. 2)"*. Set it
  from the Entities tab, or leave it blank.

  The basis is **descriptive only** and can never move the date — every route
  in Explanation 2 arrives at the same day, which is the whole reason this is a
  label rather than a second flag. `resolve_due_date()` deliberately cannot see
  it, and a test pins that.

  Nothing migrates: an unstated basis reads as the s.44AB case, which is all
  `audit_case: true` has ever meant, and an existing `entities.yaml` survives a
  save byte-identically.

### Changed
- The two audit fields on the Entities tab are relabelled **"Extended due
  date"** rather than "Audit case (s.44AB)" — the old label was the
  mislabelling itself, since it named one route to a date reached by several.

## [3.2.0] — 2026-08-03

### Added
- **Inter-entity Matrix gains the entity selector**, completing the Phase 5
  wiring that v3.1.0 explicitly left out. Its `books` input takes many books
  rather than one, which `book_from` could not serve at the time; a multi-book
  field is now simply the plural of the single-book one. Pick two or more
  entities from a multiselect dropdown and their registered books fill in, one
  path per line. Entities with no registered book are named under the field
  instead of being left as blank lines, and Browse **adds** to the list rather
  than replacing it — so books can be gathered a few at a time — and never
  lists the same book twice.

### Fixed
- **The Inter-entity Matrix could never complete a run.** Its `books` input is
  `type: "files"`, which the generic renderer served as an upload component:
  the run handler staged every picked file into a temporary directory and put
  *that directory's* path into the input map, run-args substitution flattened
  it to a single string, and the skill then saw one path where it needed two —
  returning "select at least two .gnucash books for a matrix" on every run,
  whatever you picked. The tab has been broken since it shipped. Books are now
  paths opened in place, and the skill accepts the newline-separated list.
- **The Matrix was copying live `.gnucash` books into `%TEMP%`** as part of
  that same upload staging, and refused outright any book over the 100 MB
  upload cap — a limit that exists to bound uploads and has no business
  applying to a file opened read-only where it already lies. Real books pass
  100 MB routinely.

## [3.1.2] — 2026-08-01

### Fixed
- **A filled-in book path is its own acknowledgement.** The "found a
  registered book" status line stayed on screen after the path appeared,
  restating what the field already showed — and kept showing after a manual
  override, where it was simply wrong.

### Changed
- Dependency bumps: `cffi`, `typer`, `regex`, `xxhash`, `langsmith`,
  `beautifulsoup4`, `extract-msg`, `soupsieve`, and the langchain-ecosystem
  group.

## [3.1.1] — 2026-08-01

### Fixed
- **The GnuCash book field is a path, not an upload.** Registered books live
  outside the paths Gradio is allowed to serve, so rendering the field as a
  file component made it raise an error and silently drop the value — the
  v3.1.0 auto-fill could not land a path on any surface. The field is a text
  box holding the path, with a native Browse button beside it.

### Changed
- The entity dropdown leads the form, above the book field it fills, and says
  whether a registered book was found.

## [3.1.0] — 2026-07-31

### Added
- **Entity selector auto-fills the GnuCash book on every book surface**
  (book registry, Phase 5). v3.0.0 let each entity register its `.gnucash`
  book per financial year, but only ITR Workbook consumed it — every other
  surface still demanded the path by hand, because those tabs had no entity
  context to key `resolve_book()` on. Each surface that asks for a book now
  gets an optional Entity dropdown; picking a person fills the book field
  from the registry. The file field stays visible and fully overridable —
  Browse still wins, and a registry miss (no `books:`, unregistered entity,
  or a registered path that no longer exists on disk) deliberately leaves
  the field untouched rather than blanking a path the user picked manually.
  Eight surfaces wired: Banks Convert to GnuCash, Banks Review,
  Inter-entity Reconcile (both sides), 26AS Convert to GnuCash, 26AS Review,
  KRChoksey Convert to GnuCash, KRChoksey Review, AIS Reconcile.
  Inter-entity Matrix is deliberately excluded — its `books` input is
  `type: "files"` (many books), which `book_from` cannot serve. Wiring is
  declarative (`book_from:` on a file input paired with an
  `options_from: itr_entities` select) and happens at render time via
  `.change()`, not run time. Entity inputs are UI-only; no skill `run()`
  signatures changed. Explicit non-goal: no CSV→book provenance record, so
  a wrong entity/book pairing is still possible and is not flagged.
- **"Entities" promoted to a GnuCash-level tab.** `entities.yaml` stopped
  being ITR-only data once it began feeding every Entity dropdown and every
  book auto-fill, so its editor moved up one level out of ITR and now sits
  as the last sub-tab under GnuCash. The tab also gains a read-only
  "Other dropdown data" panel explaining where the AY, bank and model lists
  come from (shipped rules + overlay, installed parsers, configured LLM
  endpoint) — none of those has a file to edit, so the panel reports rather
  than pretends to be an editor.

### Fixed
- **AIS Reconcile had no tab — it was invisible from the day it shipped.**
  The ITR group rendered `_itr_skills[0]` only, so whichever skill sorted
  first ("ITR Workbook") got a tab and the rest silently vanished. The
  indexing predates AIS Reconcile (8c91927, 2026-07-13) by two weeks, so the
  skill had been registered, documented and help-covered but unreachable in
  the UI for its entire life. The group is now iterated rather than indexed,
  and a new `test_every_registered_skill_gets_a_tab` fails CI if any future
  group repeats the mistake.

### Changed
- **Tab labels standardised so the same verb means the same thing in every
  group.** Banks "Import Statement" → "Convert to GnuCash"; Banks "Review
  Mappings" → "Review" (every group now ends in a tab called "Review"); 26AS
  "Journal" → "Convert to GnuCash" and "Journal Review" → "Review";
  KRChoksey "KRChoksey" → "Convert" and "GnuCash Import" → "Convert to
  GnuCash"; ITR "ITR Mapping" → "Review Mapping"; the top-level "Banks" tab
  → "Bank Skills" (it holds the per-bank statement parsers, while
  GnuCash → Banks holds the end-to-end book pipeline — the two were
  indistinguishable by name). AIS Reconcile now renders last within ITR.
- **"Intercompany" → "Inter-entity" in everything user-visible** — display
  names ("Inter-entity Reconcile", "Inter-entity Matrix"), report titles,
  CLI descriptions, AGENT.md and help text. "Reco" → "Reconcile"
  throughout. Internal `name:` keys, module paths and directory names are
  unchanged (`skill_gnucash_intercompany`, `reconcile_intercompany.py`), so
  nothing on disk or in the registry moves.
- **The five entity dropdowns had five different labels.** All now read
  "Entity (optional -- auto-fills the GnuCash book from the registry)"; the
  two-book Inter-entity Reconcile uses First/Second variants.

### Breaking
- **Output filename suffixes renamed** with the Inter-entity rename:
  `-Intercompany-Recon` → `-Inter-entity-Recon` and `-Intercompany-Matrix`
  → `-Inter-entity-Matrix`. No code globs those suffixes, so this only
  orphans previously generated output files — accepted deliberately.

## [3.0.0] — 2026-07-29

### Added
- **Per-entity, per-financial-year GnuCash book registry.** Each entity in
  `entities.yaml` can now record which `.gnucash` book is theirs for a given
  FY, under a `books:` map keyed by FY (`"2025-26": <path>`). A new
  `ui/_book_registry.py` answers "where is this person's book?" via
  `resolve_book(entity_key, fy)`, falling back to the newest registered FY
  when none is given, and still honouring the older single `book:` key so
  existing configs keep working. Books are registered from the Entities tab.
- **Mapping learnings are scoped per entity instead of per workbook name.**
  Rules sidecars lost their year component, so a learning taught in one
  financial year survives the roll into the next — previously a year-roll
  silently orphaned roughly 2,600 lines of accumulated mapping rules,
  because the sidecar name embedded the year. A self-heal migration
  (Phase 0b) rewrites the old year-bearing sidecar to the new name on first
  use. It fires **per book, on a GnuCash Pipeline run or a Save in Review
  Mappings** — not at app start — so a book you have not touched since
  upgrading is migrated the first time you use it, not before.
- **Native "Browse" buttons for every GnuCash book picker**, replacing
  paste-the-path-in with a real file dialog.
- Read-only book snapshots, so a reconciliation reads a consistent view.

### Changed
- **Breaking: `EntityProfile` gains `books:` and the ITR flow resolves the
  book through the registry** rather than from the workbook filename. ITR
  Workbook auto-fills an empty book field from the registry; other surfaces
  still required the path by hand until 3.1.0.
- Skill-registry count assertion updated 21 → 22.

## [2.23.1] — 2026-07-29

### Fixed
- **Scrubbed real personal data from example data, comments and tests.**
  Example configs, docstrings and test fixtures had accumulated real family
  names. All of it was replaced with synthetic placeholders. This is a
  forward-only scrub: this release is the clean waterline, and everything
  from here on is checked before it ships. Earlier history still carries the
  original values and was deliberately not rewritten.

## [2.23.0] — 2026-07-28

### Added
- **`workbook_match` is now a first-class `EntityProfile` field.** Which ITR
  workbook belongs to which entity had been inferred from the entity key or
  display name, which broke for entities whose workbook is named differently
  and needed longest-match-wins handling for the HUF. It is now declared
  explicitly in `entities.yaml` and edited from the Entities tab, so the
  match is configuration rather than a naming convention.

## [2.22.0] — 2026-07-28

### Fixed
- **Review Mapping showed another entity's proposed mappings.** Proposed
  mappings were pooled across all entities rather than scoped to the one
  being reviewed, so suggestions from an unrelated book could be accepted
  into the wrong workbook. Proposals are now entity-scoped.

### Added
- **Row delete in Review Mapping**, so a bad proposed mapping can be removed
  outright instead of only being edited or ignored.

## [2.21.1] — 2026-07-28

### Fixed
- **AIS Reconcile — books TDS-credit sign (v0.1.1).** Phase C (AIS vs
  GnuCash books) summed the TDS-credit tags straight off `account_fy_sum`,
  which applies the ITR presentation-sign normalization that FLIPS
  EXPENSE (and the other credit-normal) account types. Real family books
  post TDS to an EXPENSE account ("TDS on Interest", "TDS on Dividend")
  rather than to an ASSET receivable, so the flip turned `books_tds_credit`
  NEGATIVE and fired a spurious mismatch against the (positive) AIS/26AS
  sides on every entity. The TDS bucket is now un-flipped to debit-positive
  for FLIP_TYPES accounts, so it lands positive whether TDS is modelled as
  an EXPENSE or an ASSET receivable (ASSET was already correct). Validated
  against the FY2025-26 books: books TDS now ties to 26AS exactly for the
  entities where the book agrees (e.g. 37074.15, 11879.60). The prior tests
  only exercised ASSET-typed TDS accounts, which is why the flip escaped —
  regression tests for the EXPENSE and mixed EXPENSE+ASSET cases were added.

## [2.21.0] — 2026-07-27

### Added
- **AIS Reconcile skill (v0.1.0, alpha).** New "AIS Reconcile" skill
  (`src/agents/skill_ais_reconcile/`) decrypts an Income-Tax AIS (Annual
  Information Statement) JSON export entirely in-process — the entity is
  resolved from the export's own masked-PAN filename prefix (matched
  against `entities.yaml`'s real PANs) and the decrypt password is derived
  from that entity's PAN plus DOB (Individual) or DOI (HUF/non-individual),
  reusing `decrypt.py`'s existing PBKDF2/AES-256-CBC implementation — no
  manual password entry. Runs up to four reconciliations: (1) AIS-internal
  — l1 detail vs l2 aggregate per element, cross-section TDS-credit total;
  (2) AIS vs 26AS — TDS-credit tie-out at the aggregate, per-quarter, and
  per-income-category grain when a 26AS workbook is supplied; (3) AIS vs
  GnuCash books — the primary reconciliation, tying AIS-reported
  interest/dividend/salary income and TDS credit against what's actually
  posted in the books via nearest-ancestor account-tag resolution, when a
  matching-FY `.gnucash` book + entity mapping are supplied (a book with no
  transactions in the AIS's FY still produces a tie-out sheet but the run
  summary carries an unmissable WARNING); (4) advisory portal-feedback
  suggestions distilled from every flagged delta across the first three —
  conservative by design (confidence capped at low/medium, never high;
  ambiguous cases suggest "review" rather than a definitive portal action)
  and explicitly never auto-submitted, for a human (a CA) to review before
  acting on the AIS portal's own feedback mechanism. All four
  reconciliation/feedback modules are pure functions with no I/O;
  `agent.py` is the only file in the skill that touches the filesystem
  (reading the AIS export, `entities.yaml`, the entity's mapping file, the
  rules config, and the optional 26AS workbook). v1 scope note: the AIS
  side of the books tie-out only considers `tdsTcs`-reported income, to
  avoid double-counting against other AIS sections describing the same
  underlying transaction. Output is a single Excel workbook (Summary,
  Books Reconciliation, one sheet per AIS section, Flags, 26AS Tie-out,
  Feedback Suggestions).

## [2.20.0] — 2026-07-26

### Added
- **ITR workbook — Schedule EI (Details of Exempt Income) and a Sch.No cross-reference column on Statement of Income.** New own sheet, "Schedule EI", added to the presentation layer (rendered whenever exempt-income data is wired up; always positioned after `CG`). Sourcing is deliberately BOTH book-tagged and editable input: PPF/EPF interest, tax-free bond interest, share of firm profit (s.10(2A)), and an "other exempt" catch-all are each a live formula tying back to the hidden `ExemptIncome` engine sheet (two new tags, `EXEMPT_TAXFREE_BOND_INTEREST` and `EXEMPT_OTHER`, added alongside the pre-existing `EXEMPT_PPF_INTEREST`/`EXEMPT_10_2A`); agricultural income has no book tag and is a real editable `_input_cell`. The sheet's own `SUM` totals both halves — the formula/audit-trail principle (every money cell on a presentation sheet is either a formula or an explicit input cell) is upheld throughout. Statement of Income gains a new memo line, "Exempt income (Schedule EI)", reading from that total via `='Schedule EI'!...` — written strictly *after* the Total Income line so it structurally cannot feed the GTI/Total-Income/tax ladder (proven by a formula-graph test, not a computed number). A new "Sch." cross-reference column (column F, chosen because it already had a reserved width and sits outside the column range the existing formula-invariant tests scan) is added to Statement of Income and populated for the `CG` section heading ("CG") and the new memo line ("EI"); interest/dividend/TDS rows have no per-payer detail sheet yet (parked for a future build) so those cells are left blank rather than pointing at a sheet that doesn't exist. Per-payer interest, per-company dividend, and per-deductor TDS leaf-level detail sheets remain explicitly out of scope for this change.
- **ITR workbook — Interest Schedule, Dividend Schedule and TDS Schedule (category-level detail behind Statement of Income).** Three new presentation sheets, positioned after `Schedule EI`. `Interest Schedule` groups the "Income from other sources" interest leaves into Deposits (Bank FD + NBFC/HFC), Savings Bank (s.80TTA/80TTB-eligible) and Other (IT-refund + taxable EPF interest), each a formula tying back to `OtherSources`, plus the existing 234C quarter-bucket split rendered as a formula tie-back rather than restated numbers. `Dividend Schedule` does the same for the gross dividend leaf and its quarter split. `TDS Schedule` ties out the four TDS/TCS category totals (salary/interest/dividend/TCS) to `TaxesPaid` by formula, renders the already-itemized `unclassified_sections` rows (26AS TDS sections matching none of the Rules-config categories — section/deductor/TAN/amount, now each captured on its own cell on `TaxesPaid` for exactly this purpose) as real formula-backed rows, and carries a "Gross receipt offered" column that is laid out but populated nowhere yet (no source data carries a gross-receipt figure today). Statement of Income's Sch.No column (F) is now populated for these leaves too — "INT" for the interest leaves, "DIV" for dividend, "TDS" for the four TDS/TCS leaves (Advance Tax and Self-Assessment Tax stay unlabelled — no deductor/section detail to schedule). Every new sheet carries a single, clearly-styled note where per-payer/per-company/per-deductor detail is PARKED (the calc layer does not carry payer/deductor names through to leaf level for anything except the unclassified 26AS rows); no fabricated line items were added. Proven by formula-graph tests, same technique as Schedule EI's: each schedule's grand total is a formula over its own category cells, and Gross Total Income/Total Income/the tax computation formulas are asserted to never reference the three new sheets.
- **ITR Entities CRUD tab.** New "ITR Entities" sub-tab under GnuCash > ITR
  (alongside "ITR Workbook" and "ITR Mapping") for adding, modifying, and
  deleting taxpayer entities in `Data/itr/entities.yaml` through the UI —
  no more hand-edited YAML for the roster. Full form over every
  `EntityProfile` field: PAN (`[A-Z]{5}\d{4}[A-Z]`), status enum, dob/doi
  dates, default regime + a `regime_by_ay` per-AY editor, the `audit_case` /
  `audit_case_by_ay` fields added in v2.19.0 (#117), and `extra_items`
  (b/f losses, clubbing notes). Validation runs before every write and
  blocks bad PAN/date/enum values with an inline error; a blank entity key
  never touches disk. `configs.py` gains `dump_entities()` (a deterministic,
  sorted-key `yaml.safe_dump` re-emitting a stable header — no `ruamel.yaml`
  dependency added, per the 2026-07-23 decision) and
  `validate_entity_fields()`. Save discipline mirrors `itr_mapping_review.py`:
  a timestamped backup of `entities.yaml` before every rewrite, anchored via
  `data_root_dir()` (works in both source and frozen layouts). Renaming an
  entity's key cascades its `.mapping.yaml` file to the new name, or blocks
  the whole save with a clear message if a file already exists at the target
  name (never a half-applied rename). Delete requires a double confirmation
  and archives (never deletes) the entity's `.mapping.yaml` to
  `Data/itr/_archive/`.

### Changed
- **ITR Entities tab: filed-return rename/delete cascade.** Renaming or
  deleting an entity now cascades over its filed returns in
  `Data/ITRFiled/<entity_key><token>.{json,pdf}` (e.g. `Harshal2425.json`),
  replacing the earlier manual-check note. `configs.find_filed_returns()`
  matches files by entity-key prefix only — the trailing `<token>` is a
  human filing-batch label, never a parsed/trusted assessment year — with a
  strict boundary rule so prefix-colliding keys (`Bob` vs
  `BobHUF`) never cross-match. Rename swaps the leading entity-key
  segment of each matched filename (token + extension preserved) and blocks
  the *entire* save, same as the `.mapping.yaml` cascade, if any target
  filename already exists. Delete archives (moves, timestamped) each
  matched file to `Data/itr/_archive/` alongside the mapping-file archive —
  filed returns are never deleted, only moved.
- **ITR Entities tab: atomic save/delete cascade (review fix).** Save and
  delete now run the filesystem cascade (`.mapping.yaml` rename/archive +
  filed-return renames/archives) *before* rewriting `entities.yaml`, and
  roll every completed move back if a later step in the cascade — or the
  final `entities.yaml` write itself — fails, surfacing a clean Gradio
  error instead of a stack trace. Previously `entities.yaml` was rewritten
  first with no exception handling around the cascade, so a mid-cascade OS
  failure (locked file, AV, permission error) could leave `entities.yaml`
  updated while the on-disk mapping/filed-return files were still under
  their old names. `find_filed_returns()` also gains an optional
  `all_entity_keys` param for longest-match disambiguation of
  digit-extended key collisions (e.g. `Prop1` vs `Prop12`) and matches
  filenames case-insensitively (Windows casing).

## [Unreleased]

> **Note.** The entries below were never rolled into a released section at
> tag time, but git history shows they already shipped: the help-system
> entries in `v1.2.2` (`a284dc3`, `57975da`), the HDFC/ICICI value-date
> entries in `v2.3.0` (`cc2221e`, PR #62), and the Intercompany sub-tab
> entry in `v2.3.1` (`a8cf73a`, PR #76). They are left here rather than
> back-filed into sections that do not exist for those tags.

### Added
- **Help system, single-source-of-truth.** Every skill's user help now lives in
  a `help:` block in its `skill.yaml` (overview, when-to-use, per-input
  tooltips/formats/gotchas, steps, per-output-file interpretation, tips,
  troubleshooting). One generator, `scripts/gen_docs.py`, renders it to per-skill
  guides in `docs/user-guide/`, a bundled standalone `docs/USER-GUIDE.html`, and
  the developer `docs/dev/skills-reference.md`.
- **In-app help.** A collapsible "How to use — formats & output" panel on every
  skill tab, a central **Help** tab, and two-tier tooltips (native `info=` helper
  text on inputs; `title=` hover on each output file). All read the `help:` block
  live via `agents.registry` (new `SkillHelp` model) — see `ui/_help.py`.
- **Docs.** `docs/dev/help-block-schema.md` and `docs/dev/editing-help.md`;
  `USER-GUIDE.html` bundled into the frozen package via `paskills.spec`.
- **CI.** `tests/test_help_coverage.py` fails if any UI skill lacks help or if
  the generated docs are stale (`gen_docs.py --check`).

### Fixed
- **HDFC — Value Dt now used on every input path.** HDFC statements carry
  both a posting Date and a Value Dt; the canonical CSV's "Date" column
  (which flows unchanged through balance checks, dedup, and account mapping)
  now emits the Value Dt on the PDF text path (`skill_hdfc`) and the PDF OCR
  path, matching the XLS/XLSX path which already preferred it. Falls back to
  the posting date only when Value Dt is blank. Note: opening-balance
  reconciliation and duplicate detection key on this field, so rows where
  posting and value dates differ (e.g. cheque clearing) may now be bucketed
  by a different date than before.
- **ICICI — docstring corrected.** The module docstring wrongly claimed
  ICICI used Transaction Date; the code already preferred Value Date
  (falling back to Transaction Date only when blank). No behavior change —
  documentation and a regression test now match the existing code.
- **Intercompany skills moved out of GnuCash > Banks.** "Intercompany Reco"
  and "Intercompany Matrix" are not bank-statement tools and were rendering
  alongside statement-import skills under the Banks sub-tab. Both now use a
  dedicated `category: "intercompany"` and render under a new
  GnuCash > Intercompany sub-tab (Reco first, Matrix second). Banks now shows
  only statement import + Review Mappings.
- **ITR workbook — refund/tax-payable line raised #NUM! for every tax-payable or loss-year return.** Excel's `MROUND(number, multiple)` raises `#NUM!` whenever `number` and `multiple` have opposite signs. The s.288B refund/tax-payable rounding (`Statement of Income` and `Computation` sheets) and the s.288A Total Income rounding always round to a positive Rules-sheet constant, but the number being rounded goes negative for a tax-payable (as opposed to refund) assessee or a loss year — so every such return produced a broken `#NUM!` cell instead of a number. Replaced with a new sign-safe `presentation.mround_safe()` helper (`ROUND((x)/m,0)*m`), which reproduces `MROUND`'s round-half-away-from-zero behaviour for both signs and never errors, at all three call sites.
- **ITR workbook — Salary sheet's displayed gross silently dropped perquisites.** The Salary sheet's "Gross salary (17(1)+17(2)+17(3))" label showed the book's `SALARY_GROSS` tag total, which only ever captures 17(1) — so any salary with non-zero 17(2) perquisites or 17(3) profits-in-lieu displayed a gross that didn't match its own label (and didn't match Form16). `build_salary()` now sources `gross` from Form16's `total_1d` (falling back to `s17_1+s17_2+s17_3` when `total_1d` is absent) on the Form16 path; the book-only path (no Form16) is unchanged. `verify.py`'s Book↔Form16 cross-check control is untouched and continues to compare the book's `SALARY_GROSS` total against 17(1) alone. Added a matching fail-loud "banner, no abort" control (`SalarySchedule.reconciliation_ok`/`reconciliation_diff`, mirroring the existing Capital Gains reconciliation banner): a genuine gross/exemptions/deductions vs income-chargeable mismatch now surfaces a prominent ERROR banner on the Salary and Statement of Income sheets and a Reconciliation-sheet line, and is picked up by `agent.py`'s exit-code logic — the workbook is still always fully written.
- **ITR workbook — Statement of Income now leads with income, not working machinery.** The "Brought forward losses set off" input block and the New/Old regime tax-working formulas used to sit at the very top of the `Statement of Income` sheet, ahead of "Income from Salary" — so a reader had to scroll past raw working cells before reaching any income figure. Both now live in a new "Workings / Inputs" section below "Refund Due / (Tax Payable)", ahead of the Assumptions block. The b/f-loss cells remain live, directly editable inputs (default 0); the regime tax-working formulas remain on the same sheet. A short pointer note ("see Workings below") was added to the House Property, Business, and Capital Gains section headings, which apply b/f set-off. The move is self-proving: presentation.py computes the new section's rows analytically ahead of time and asserts the real render lands exactly there, turning any future layout drift into an immediate generation-time `AssertionError` instead of a silent `#REF!`.
- **ITR workbook — Assumptions block now notes interest u/s 234A/234B/234C is not computed.** The tax-payable figure shown was always pre-interest; this was previously undocumented on the deliverable page itself. 234-interest computation remains out of scope.
- **CI now collects `src/agents/**/test_*.py`, not just `tests/`.** Bare
  `pytest` used `testpaths = ["tests"]`, so `skill_gnucash_import`'s
  `test_transforms.py` (20 tests) was silently skipped in every run — it only
  ran when invoked by explicit path. `testpaths` now also includes
  `src/agents`. That test file also did `sys.path.insert(...)` +
  `import agent`, writing the generic `agent` key into `sys.modules`; when
  collected alongside `tests/skill_itr_workbook/test_agent_full_pipeline.py`
  (which does the same for its own, different `agent.py`), whichever loaded
  first "won" and the other failed with
  `ImportError: cannot import name 'parse_date' from 'agent'`. Fixed by
  loading `skill_gnucash_import`'s `agent.py` via `importlib` under a unique
  module name instead, so it no longer collides with other skills' same-named
  `agent.py` modules regardless of collection order.

## [2.19.0] — 2026-07-25

### Added
- **ITR statutory rules now ship *inside* the app and update with every release.**
  (#118) The five canonical rule files (`tax_rules_AY2025-26.yaml`,
  `tax_rules_AY2026-27.yaml`, `user_rules.yaml`, and the `entities.example.yaml` /
  `scrips.example.yaml` templates) moved out of the git-tracked `Data\itr\rules`
  tree into a new `bundling/canonical/itr/` source tree that is bundled into the
  frozen build (`_MEIPASS/itr/`, via `paskills.spec` `datas`) and read directly at
  runtime. `Data\itr\rules` is now a normally-empty, read-time **overlay**:
  `rules.load_rules()` / `load_user_rules()` accept either a single directory
  (unchanged, hermetic call shape the tests rely on) or an ordered
  `[overlay, canonical_base]` search list where the first per-AY match wins, so a
  file dropped into `Data\itr\rules` still overrides the shipped copy for
  hand-tuning one book. Rationale: the PortableApps.com Launcher copies
  `DefaultData\` into `Data\` only once, on first run, so statutory fixes shipped
  there would freeze forever after a user's first launch — canonical rules now
  live outside `DefaultData` and are replaced wholesale each update. The AY
  dropdown (`ui/tabs/_generic.py`) now scans base ∪ overlay (deduped by
  `meta.fy`, overlay wins) so it populates from shipped rules even with an empty
  overlay. `.gitignore` collapses to a single `Data/.gitkeep` carve-out —
  `git ls-files Data/` now returns nothing, so `Data\` is 100% user data with no
  allow-list negations a stray PII file could slip through. New regression test
  proves overlay-wins.

### Changed
- **ITR s.234A/B/C interest — the filing due date is resolved from config, not a
  hardcoded 31 July.** (#117) The interest calc previously hardcoded 31 July as
  the furnishing due date for every entity, so an audit-case return (statutory
  31 October) was computed against the wrong due date and the Assumptions note
  asserted "31 July, i.e. the non-audit case" even on an audit workbook.
  `schedules.py` now calls `resolve_due_date(rules, year_key, audit_case)`,
  reading `common.filing_due_dates` from the rules file with a 31-Jul / 31-Oct
  fallback (a CBDT extension can override per-AY); `EntityProfile` gains
  `audit_case` / `audit_case_by_ay` (mirroring `default_regime` / `regime_by_ay`),
  resolved per-AY in `agent.py` and threaded through `build_all_schedules` ->
  `build_interest_234`; and the Assumptions note prints the actual resolved due
  date. Both shipped `tax_rules_*.yaml` gain an optional `common.filing_due_dates`
  block.

## [2.18.2] — 2026-07-24

### Fixed
- **ITR workbook — 26AS silently dropped s.193 TDS from the TaxesPaid tie-out.**
  `tds_sections.interest` in both `tax_rules_AY2025-26.yaml` and
  `tax_rules_AY2026-27.yaml` listed only `194A` (interest other than
  securities); `193` (interest on securities/bonds) was never classified, so
  any s.193 TDS credit was silently excluded from `TaxesPaidSchedule`,
  understating the TDS credit and overstating tax payable and the 234B/234C
  interest that would be computed on it. Added `193` to both Rules-config
  files alongside `194A`, with a comment distinguishing the two. Also added a
  visible-but-non-fatal guard for the underlying class of bug, not just this
  one section: `build_taxes_paid()` now records every 26AS Part I transaction
  whose section code is in *none* of the Rules-config `tds_sections`
  categories and whose `tax_deducted` is non-zero
  (`TaxesPaidSchedule.unclassified_sections`) — a WARNING banner (amber, not
  red) is written on the Statement of Income and TaxesPaid sheets naming the
  section code(s) and amount at stake, and `agent.py`'s run summary reports
  it on stderr. Unlike the CG-reconciliation ERROR banner, this is
  deliberately non-fatal and does **not** set a non-zero exit code — an
  early version of this fix treated *any* section outside `interest`/
  `dividend` as unclassified, which meant every ordinary s.192 salary-TDS row
  (present in every salaried taxpayer's 26AS) tripped a false-positive ERROR
  and exit-1 on a completely correct workbook. `tds_sections` now also
  carries a `salary: ["192", "192A"]` category — recognised as accounted-for
  (reconciled separately via Form16 / the Salary schedule) rather than fed
  into this interest/dividend tie-out — so a section landing in *any*
  `tds_sections` category, not just `interest`/`dividend`, is treated as
  classified. TCS (206C..., added in #112) was checked and confirmed to
  never reach this guard at all: it lives on the 26AS workbook's separate
  "Part VI" sheet, and `as26.parse_as26_workbook()` only ever reads "Part I",
  so no TCS codes were added to `tds_sections`. An unrecognised section with
  zero TDS at stake stays silent — nothing is at stake, so nothing to flag.
  Nothing is hardcoded: the fix is a config addition plus a guard against the
  next unclassified section, not a hardcoded `"193"`/`"192"` anywhere in
  Python.
- **ITR workbook — a renamed GnuCash account produced a path-drift warning
  that never cleared.** `configs.load_mapping()` compares a mapping entry's
  stored `path:` against the parsed tree's current path per GUID and warns
  on a mismatch (rename detection), but nothing ever wrote the refreshed
  path back — so once an account was renamed, the same warning fired on
  every single run forever, indistinguishable in the logs from a genuine
  problem. GUID is identity; `path:` is just descriptive metadata, so
  `apply_mapping_corrections.py` now refreshes every drifted path it can
  confirm against the reviewed workbook's current guid → path state
  whenever it next writes the mapping file (a correction run "heals for
  free"), and a new `--refresh-paths` mode lets drift clear on its own with
  no tag correction pending. This is refresh-*on-write*, not
  auto-write-on-run: a plain workbook build never mutates the mapping file.
  A GUID missing from the tree entirely (deleted account, or the wrong book
  loaded) is a different, real problem and is never auto-healed — it keeps
  warning loudly, worded distinctly from the now-self-healing rename case,
  until a human resolves it.

## [2.18.1] — 2026-07-23

### Added
- **The 26AS Journal review screen now regenerates a Part-I-only journal on every
  save.** Part II (15G/15H) rows carry their own `15GJ` transaction-ID series, and
  in books where those entries were posted by hand they must be dropped before
  import or the reclassification double-books. Filtering them out was a manual
  step, and worse, a step that silently expired: the review screen rewrites the
  *full* journal, so any hand-filtered import-ready copy went stale the moment
  anyone used the screen. The screen now writes a sibling
  `…-tds-journals-partI.csv` alongside the full journal every time it saves, with
  its own download button, so the filtered file can never lag the reviewed one.
  Partitioning happens per whole transaction — a transaction is assigned to Part I
  or Part II once, by its Transaction ID, so a multi-split entry can never be
  half-dropped — and both sides are re-verified to sum to zero. When a run has no
  Part II rows at all, no sibling is written and any stale one from a previous save
  is deleted rather than left behind.

### Fixed
- **The Part I split's error path claimed the full journal was safe to import.**
  If the splitter failed to load, the save handler could not distinguish "this run
  has no Part II rows" from "the split never ran" — both left the output path
  unset — and fell into the reassuring branch, telling the user the full journal
  was the only file to import. That is precisely the double-booking the feature
  exists to prevent, reintroduced inside its own error handler, and it left a stale
  `-partI.csv` on disk to be mistaken for a fresh one. Failure is now tracked
  explicitly rather than inferred from a missing path: the screen says plainly that
  the split could not be regenerated, refuses to name any file as safe, and deletes
  the stale sibling (reporting the exact path if the delete itself fails).

## [2.18.0] — 2026-07-23

### Added
- **One shared review engine for all four review screens, `ui/_review_engine.py`.**
  Bank matching, ITR mapping, 26AS TDS journals, and KRC/GnuCash review had each
  grown their own copy of the same row-rendering/picker/save-round-trip
  machinery, including the two Gradio workarounds needed to make any of it work
  (`gr.HTML` strips `<script>`; `gr.State` has no DOM node, so the save payload
  rides a hidden textbox). A bug fixed in one screen stayed broken in the other
  three. All four screens now run on the one engine, and per-row presentation
  (tags, row class, badges, locked state, notes) is computed in Python instead
  of JavaScript, so it is unit-testable — the screens are now covered by tests
  that actually render them, closing a gap where a `NameError` had previously
  shipped with a green suite because nothing exercised the render path.
- **26AS TDS journals and KRC/GnuCash now have a review UI.** Both previously
  had no screen at all — a row flagged "needs review" that nothing lets you
  actually review is just a file nobody opens.
- **Native binary resolution, `src/agents/_native_resolve.py`.** Resolves
  vendored Poppler/qpdf/Tesseract to absolute paths for the standalone skill
  scripts, which run as subprocesses and cannot import from `ui/`. Prompted by
  an incident where Xpdf's `pdftotext` sat earlier on PATH than Poppler's:
  both binaries share the same name, both exit 0, both emit text — only
  `-layout` column spacing differs, and the only reliable way to tell them
  apart is the version banner on stderr — so the resulting 26AS extraction
  silently produced zero deductor rows with no error at all. An identity gate
  now verifies the resolved `pdftotext` really is Poppler and fails loudly,
  naming what it found and where, instead of quietly falling back.
- **26AS Part II (15G/15H) parsing.** Previously ignored entirely. Now parsed
  and routed into TDS journals as reclassification entries, under its own
  transaction-ID series so the GnuCash importer keeps it separate from Part I.

### Changed
- **PATH injection for native binaries now runs unconditionally for every
  skill**, rather than only for skills that declared a native dependency in
  their manifest — the declaration itself was the thing most likely to be
  wrong. Manifests were also corrected against what the scripts actually
  invoke, and the remaining bare-name shellouts converted to go through the
  resolver; one of them sat behind a correct manifest and would have failed
  only when run standalone.

### Fixed
- **Review screen saves now re-read from disk and re-validate server-side**
  instead of trusting the posted payload: row identity, whether the row is
  editable, and whether the chosen account actually exists are all checked
  again at save time. Rejections are reported per row rather than swallowed.
- **Unknown input on a review screen now classifies to the locked branch,
  never the editable one.** If the underlying reason text changes and the
  classifier stops recognising it, the screen refuses to edit rather than
  quietly allowing an edit it shouldn't.
- **26AS extractor refuses to write a vacuous extraction.** An extraction
  with no rows in any part now raises instead of silently producing an empty
  workbook — the failure mode that motivated the native-binary identity gate
  above.

## [2.17.0] — 2026-07-22

### Added
- **Balance Sheet now tallies against the Income Statement.** A GnuCash
  balance sheet exported mid-year cannot balance on its own: the year's
  income and expenses are still sitting in the Income Statement and have not
  been closed to capital, so Assets exceed Liabilities + Equity by exactly
  the net income — previously shown as an unexplained gap, which on a
  document handed to a CA or a bank reads as an error in the books. The IS
  now closes with a bottom line, "Net Income / (Loss) for the Year"; the BS
  brings that figure over under Equity as a live reference, restates "Total
  Equity including Current Year Income", and closes with "Total Liabilities
  and Equity" and a difference row against Total Assets. The difference row
  is printed rather than left to the reader, so if a later change breaks the
  tie the sheet says so instead of looking clean.

### Changed
- **BS/IS sections are located by name rather than by position**, so a book
  with a different top-level section order still tallies against the right
  sections. When there is no IS bottom line to source from, the tally rows
  are omitted rather than showing a tie that quietly treats the year's
  income as nil.
- **Docs reorganised behind a new index**, and the "Closing the app"
  instructions removed from the README and from release notes going
  forward.

## [2.16.0] — 2026-07-22

### Added
- **TCS now flows end to end, from 26AS Part VI through to the tax
  computation.** It was being lost at the first step: Part VI of Form 26AS
  was never extracted, so the credit never reached the workbook, never
  reached GnuCash, and never reduced tax payable — understating taxes paid
  and overstating the balance due, the same as leaving out a TDS credit.
  Convert now parses and renders Part VI (TCS) with per-collector
  sub-totals and the same reconciliation guard Part I already had. Journal
  now builds TCS entries — Dr the TCS account, Cr Drawings, tax only, since
  the spend it was collected on is already in the books — with both
  accounts resolved from the entity's own chart and the credit leg
  configurable for TCS paid across separately rather than bundled into a
  purchase. To use it: map `TAXPAID_TCS` to the TCS account in the entity's
  mapping YAML; until that mapping is added, nothing flows into the ITR
  workbook.

### Changed
- **234A/B/C workings now disclose that the "26AS" TDS credit figure
  includes salary TDS and TCS on their BOOK figures**, since the 26AS reader
  classifies only interest and dividend. The Statement of Income and the
  234A/B/C charges already counted TCS correctly, so no computation change
  was needed there — this is a disclosure-only addition to the Workings
  section.

### Fixed
- **Convert help text corrected.** It claimed the workbook lays out advance
  tax and self-assessment tax; a modern Form 26AS carries neither.

## [2.15.0] — 2026-07-22

### Added
- **ITR workbook — interest u/s 234A / 234B / 234C.** The Statement of
  Income now computes interest, and the headline Refund / (Tax Payable)
  figure is net of it; previously the workbook stopped at the tax liability
  and understated what was actually due at filing. Covers 234A late
  furnishing, 234B advance-tax default (90% cliff), and 234C instalment
  deferment (15/45/75/100% with the 12%/36% safe harbours), with Rule 119A
  (base rounds down to the nearest 100) and part-month-counts-as-full
  applied throughout. The s.234C first proviso is derived from each lot's
  actual sale date, so a March capital gain does not retrospectively short
  the June instalment and an April gain gets no relief at all. Form 26AS is
  used as the TDS credit basis when available, since 234B/234C are charged
  on tax less TDS and crediting book TDS that 26AS does not support would
  understate the interest the department will actually compute. Rendered as
  live Excel formulas over editable inputs (filing date, due date, TDS
  credit, advance tax, per-instalment figures) in a new Workings section, so
  changing the filing date moves the interest and the refund.

### Known limitations
- The due date defaults to 31 July (non-audit) and is editable rather than
  driven by per-entity config.
- Instalment-wise advance tax is derived from the book's posting dates,
  since a modern Form 26AS no longer carries advance-tax challans at all.

## [2.14.0] — 2026-07-21

### Changed
- **ITR workbook — income totals AND the standard tax computation now
  compute on the deliverable page, so manual overrides propagate end to
  end.** The `Statement of Income` sheet's income ladder (Gross Total Income
  → Chapter VI-A → Total Income, plus the normal-income / special-rate-CG
  split) **and** the tax computation itself (Tax on total income (slab) →
  less s.87A rebate → add Surcharge → less Marginal relief → add Health &
  Education Cess → Total tax liability → add special-rate CG tax → less
  prepaid taxes → Refund/(Payable)) are now built from live on-page Excel
  formulas over on-page cells, instead of mirroring a hidden `Computation`
  working sheet. `Computation` keeps its full slab/rebate/surcharge/cess
  machinery, re-anchored to read the page's own normal-income cell, and
  stays as a parallel hidden backing/audit sheet — but the page's own tax
  lines are now independently live, not a mirror of `Computation`'s output.
  Special-rate LTCG/STCG (111A/112A) stays carved out of the slab base
  (regression-tested).
  **Brought-forward-loss set-off is now FOUR statutory, editable, per-bucket
  input cells** — b/f House Property loss (s.71B), b/f Business loss (s.72),
  b/f Short-term capital loss (s.74), b/f Long-term capital loss (s.74) —
  replacing the previous single lump cell and its previously-parked
  placeholder. Each bucket sets off only against its own income
  head/gain-type, capped at that head's available income for the year, at
  the head level *before* aggregation into Gross Total Income (STCL sets
  off against STCG first with any remainder spilling to LTCG; LTCL sets off
  against LTCG only) — matching what the Act actually requires rather than
  a lump Total-Income deduction. An entered amount always stays visible even
  if it exceeds the available income in its head; only its *effect* is
  capped. No change to any tax rate, rule, or the default (no-override)
  figures — the generated workbook reconciles to the same numbers as
  before. Design: `docs/history/2026-07-20-itr-onpage-totals-plan.md` (section 11,
  "REVISION 2").

## [2.13.2] — 2026-07-20

### Fixed
- **ITR workbook — capital gains understated to ~zero on every equity sale.**
  The lot-reconstruction engine (`scripts/lots.py` `_sale_transactions`)
  identified the booked-gain leg of a stock disposal by requiring the income
  split's GnuCash `action` to be `"LTCG"`/`"STCG"`. Real books never carry
  that: GnuCash only auto-stamps `Buy`/`Sell` via the stock assistant, and a
  manually entered capital-gain income split has no `action`. So the gain leg
  was misclassified as a *proceeds* split, where its negative value cancelled
  the real broker proceeds — collapsing computed proceeds to the cost basis
  and the gain to ~0 for every sale. The gain leg is now detected by account
  **type** (`INCOME`) instead of `action`; books that do set an explicit
  `action` still classify correctly. Verified against a real book: a disposal
  that previously reported a 0 gain now reports the correct ~2.46 lakh gain.
- **Capital-gains reconciliation now fails loud (banner, no abort).** When the
  reconstructed lot gains do not reconcile to the books' `CG_*_CONTROL`
  totals, the workbook is still written in full, but a prominent ERROR banner
  is placed at the top of both the `CG` and `Statement of Income` sheets, and
  the new `agent.main()` CLI wrapper exits non-zero with a stderr line. Prior
  behaviour surfaced a mismatch only as a buried `OK`/`MISMATCH` cell on a
  working sheet, so a materially wrong return could be handed on unnoticed.

## [2.13.1] — 2026-07-19

### Fixed
- **Frozen build could not start (v2.13.0 was unusable).** `pydantic` 2.13.4
  declares `_COMPATIBLE_PYDANTIC_CORE_VERSION = 2.46.4` and raises
  `SystemError` at import when it finds a different `pydantic-core`, so every
  launch of the v2.13.0 package died in `gradio` → `fastapi` → `pydantic`
  before the UI came up. Dependabot PR #69 had bumped `pydantic-core` to
  2.47.0 on its own without bumping `pydantic`; because `bundling/build.py`
  installs the lock with `--require-hashes --no-deps`, pip never evaluated
  `pydantic`'s pin on `pydantic-core` and the mismatch installed silently.
  The test suite runs against the separate dev virtualenv, so it stayed green
  — only the release workflow's frozen smoke test caught this. `pydantic-core`
  is pinned back to 2.46.4. No application code changed; v2.13.1 is v2.13.0
  with a working package.

## [2.13.0] — 2026-07-19

### Added
- **ITR workbook — `PL for Business` sheet, subtree-driven.** A fifth
  presentable sheet nets an entity's business income against business
  expenses for entities that have both: `Remuneration from Partnership` and a
  nested `Business Expenses/` group. It is driven entirely by a new optional
  entity field, `EntityProfile.business_subtree` (e.g.
  `"Income/xBusiness Income"`) — a GnuCash account path prefix walked as a
  plain subtree (`path.startswith(prefix)`), never a keyword or account-name
  match, so a business-sounding account outside the configured subtree (e.g.
  `Expense/Professional Tax`) is never swept in. Reuses the existing
  `_write_hierarchy_sheet`/`build_hierarchy`/`render_hierarchy` engine
  unchanged (extended with an optional `extra_row_fn` hook to add the "Net
  Business Income / (Loss)" total row) — no second layout engine. The sheet
  is omitted, per-run and per-FY exactly like `CG`, when the entity has no
  `business_subtree` configured or the FY has no matching activity; if
  `business_subtree` **is** configured but nothing under it appears for the
  FY, generation now raises (`BusinessSubtreeError`) rather than silently
  rendering a zero sheet, so a GnuCash account rename can't quietly drop a
  real business year. Sheet order is now `Statement of Income`, `BS`, `IS`,
  `PL for Business`, `CG`. Presentation-only: business income is not routed
  into tax computation, and no other sheet's content, order relative to each
  other, or computed figures changed.
- **ITR workbook — Father's Name, Aadhaar and real residency, unparked.**
  Three placeholders on `Statement of Income`'s header block are now live,
  optional entity fields: `father_name` and `aadhaar` (Aadhaar rendered
  space-grouped `NNNN NNNN NNNN`, CA-file style, as a formula over the raw
  digits on `Entity` — never a second literal copy). Both stay PARKED
  (styled-empty, label keeps "(to be filled)") when absent on the entity, and
  drop the parked note per field the moment a value is supplied. Stored the
  same way as PAN/DOB (plaintext in `EntityProfile`/`entities.yaml`) — no
  at-rest protection exists for any identity field in this project, and this
  does not introduce one. Residential status (`R/OR` / `RNOR` / `NR`) is now
  a DECLARED entity field (`rules.resolve_residency()`), read from the
  pre-existing but previously-unconsumed `EntityProfile.residency`; only the
  exact statutory tokens count as declared, everything else (including the
  ubiquitous legacy free text `"Resident"`) is undeclared and defaults to
  `R/OR`, preserving prior behavior byte-for-byte. The "Assumptions" footnote
  now renders only while residency is defaulted, and disappears once an
  entity declares one of the three tokens. Brought-forward loss set-off
  remains PARKED — out of scope for this change.
- **ITR workbook — four presentable deliverable sheets.** The generated
  workbook was a calculation engine, not something that could be handed to a
  CA: `Computation` was a flat two-column list with no column widths at all,
  no header block, no print setup, and it showed both regimes side by side.
  Four new sheets now sit in front of the existing ones — `Statement of
  Income`, `IS`, `BS` and `CG` — modelled on the CA-prepared reference
  workbooks, with a letterhead header block, tiered money columns, Arial 10,
  Indian digit grouping, explicit column widths sized to the longest label
  actually present, borders, freeze panes, gridlines off, and A4
  fit-to-one-page-wide print setup. The four raw working sheets (`Rules`,
  `Mapping Review`, `IS_Transcript`, `BS_Transcript`) are now hidden — hidden,
  not deleted. This is a rendering change only: no computation, rule, rate or
  tax logic was touched, and no existing sheet's values changed.
  - **Every money cell on the four new sheets is a formula** into the existing
    sheets (`Computation`, `CapitalGains`, `OtherSources`, `IS_Transcript`,
    `BS_Transcript`, `TaxesPaid`, `Entity`). Nothing is recomputed or
    hardcoded, so the audit trail survives into the printable output.
  - `IS`/`BS` rebuild the full GnuCash hierarchy by splitting the transcripts'
    `Path` column on `/`, preserving every intermediate group as its own row
    with its own subtotal. Sibling groups are never merged — in particular
    `Fixed Deposits` stays a sibling of `Cash and Bank`. (Schedule AL's
    statutory buckets do combine them; that is a different sheet with a
    different purpose.) Depth is derived from the path, not a fixed level
    count.
  - `CG` is a view over `CapitalGains`. It deliberately does not copy two
    traits of the CA reference: inline 31-Jan-2018 FMV price literals, and
    grandfathering arithmetic that is inconsistent between rows.
  - `CG` is omitted entirely when the financial year being generated has no
    capital-gains activity, mirroring what the CA produced for such a year.
    The test is per-run and per-FY — never an entity-level flag or a cached
    answer that could silently drop a real CG sheet the year it matters.
  - Three items render as a label plus an empty, visibly-styled cell rather
    than being invented or dropped: Father's Name, Aadhaar No. and
    brought-forward loss set-off. Residential status renders the assumed
    constant `R/OR` with a footnote marker and an Assumptions note, because it
    is an assumption the tool does not determine. The age half of the status
    line comes from the existing `rules.resolve_age_class()` — no new age
    logic.

### Fixed
- **ITR rules — senior-citizen age-class benefit no longer leaks to
  non-residents.** `rules.resolve_age_class()`'s docstring always claimed it
  "applies only to resident Individuals," but the code only checked `status`,
  never residency — a non-resident senior/super-senior citizen wrongly
  received the higher basic exemption (300000/500000 vs 250000), which is a
  resident-only benefit. Now gated on residency too (`NR` → `'general'`;
  `RNOR`, a resident sub-status under s.6, is unaffected). A regression test
  proves NR-65 → general slabs while resident-65 → senior and resident-82 →
  super-senior still resolve as before. Every real entity on file declares
  the legacy `"Resident"` value (undeclared, defaults to `R/OR`), so this fix
  changes no real entity's computed numbers — confirmed by direct
  before/after comparison of `resolve_age_class()` across all five.
- **ITR mapping — approved corrections now actually reach a run.** The root
  cause of a real entity showing almost every mapped account as `heuristic`
  despite real review work: `apply_mapping_corrections.py`'s CLI wrote
  corrections to a separate output file instead of the live mapping file,
  requiring a manual rename step that was never performed. It now defaults
  to writing in place, with an automatic timestamped backup; an explicit
  output path remains available as an opt-in dry run. Matching was already
  GUID-based (rename-safe) — a regression test now proves it, alongside a
  persist-reload-apply round-trip test.
- **ITR mapping — fail-loud on guessed tags.** The run summary now states
  heuristic-vs-approved tag counts and prominently warns when any INCOME
  account resolved via an unreviewed heuristic guess, in addition to the
  existing `Mapping Review` sheet detail; the Reconciliation sheet also
  gained a "Mapping provenance" block for the same reason.
- **ITR schedules — 80TTA/80TTB no longer includes NBFC/HFC deposit
  interest.** For senior/super-senior filers, the deduction base wrongly
  summed savings + bank FD + NBFC/HFC interest; 80TTB (like 80TTA) only
  ever covers banks/co-operative societies/post office deposits, never
  NBFC/HFC. Fixed with a regression test; savings, bank-FD, and NBFC/HFC
  interest were already tracked on separate lines and already correctly
  excluded PPF interest and NCD/securities routing from Schedule AL and
  ExemptIncome — added synthetic tests confirming that tag-driven routing
  was already correct, since the real defects there turned out to be
  per-entity mapping-file mistags outside this project's `Data/` fence,
  not code bugs.

## [2.12.0] — 2026-07-19

### Fixed
- **Bank abstraction, P3b follow-up — legacy `run()` UI path now routes
  through the shared consolidator.** P3b deliberately left each bank's
  legacy standalone-UI-tab `run()` entry point untouched, but BoB's and
  ICICI's tabs turned out to be reachable multi-file paths: the generic
  runner stages uploads into a temp directory and `run()` iterates every
  file there, still doing the old naive `sorted(glob)` + blind concat. That
  silently misordered batches and never surfaced missing or overlapping
  periods — worse than the pipeline case, since temp-staging filenames bear
  no relation to statement chronology. Both `skill_bob.agent.run()` and
  `skill_icici.agent.run()` now build `StatementGroup`s per file and route
  through the same `bank_common.consolidate()` / `check_continuity()` helper
  that `BankSkill.parse()` already uses, so multi-file uploads are ordered
  by actual transaction date and gap/overlap warnings are surfaced in
  `run()`'s returned summary text. ICICI reuses `_read_canonical_csv()`
  verbatim (its per-file intermediates are already canonical); BoB builds
  its own group-construction block, since its intermediates are bank-native
  rather than canonical, though both go through the identical
  `StatementGroup` / `consolidate()` contract. A single-file batch — the
  dominant real-world case — remains a proven no-op for both banks.
  `BankSkill.parse()`, `bank_common/consolidate.py`, and every bank's
  single-statement extraction/parse/OCR path are untouched; HDFC and Kotak
  have no multi-file `run()` path and are unaffected.
- **BoB batch-mode line terminators normalized to CRLF.** The `_merge_csvs`
  helper that the change above replaced read part-CSVs with universal-newline
  translation and wrote with `newline=""`, emitting bare-LF output — the
  lone outlier in this codebase. Every other CSV writer, including BoB's own
  single-file fast path (`extract_bob_statement.write_csv`), both ICICI
  paths, and `canonical_io.write_canonical_csv` (which backs the pipeline's
  `parse()` output), already defaults to `csv.DictWriter`'s standard CRLF.
  BoB batch output now matches them. No production code change was required
  for this — the new `run()` code already emitted CRLF; what changed is that
  the single-file no-op tests for both banks now assert via `read_bytes()`
  against a committed pre-#92 golden
  (`tests/skill_bob/golden_single_file_run.csv`,
  `tests/skill_icici/golden_single_file_run.csv`, captured from each bank's
  direct single-file path and unchanged since `main`). The previous
  assertion compared two post-change paths via `read_text()`, silently
  folding CRLF into LF on read, and so could not have caught this in either
  direction. No row or value content changed for either bank. (PR #92,
  `48cc688`.)

## [2.11.0] — 2026-07-18

### Added
- **Bank abstraction, P3b — shared multi-statement consolidation.** New
  `agents/bank_common/consolidate.py` (`StatementGroup`, `consolidate()`,
  `check_continuity()`) lifts HSBC's reference multi-file logic — order
  statements by actual transaction date (not filename), flag gaps (>3 days)
  and overlaps (<-1 days) as warnings without raising — into a pure,
  bank-agnostic helper. HSBC's own runtime (`skill_hsbc/scripts/parse_tsv.py`)
  is routed through it via a `sys.path` bootstrap (the script runs as a
  subprocess, so it can't rely on `agents` being importable — same convention
  already used by `skill_bob/scripts/extract_bob_statement.py`); its old
  inline sort/continuity/concat logic is gone, replaced by a thin call into
  the shared helper, with `check_statement_continuity()`'s public signature
  preserved for backward compatibility. BoB (`skill_bob/agent.py`) and ICICI
  (`skill_icici/agent.py`) now route their multi-file `BankSkill.parse()`
  batches through the same helper too, replacing their previous naive
  `sorted(glob)` + blind-concat merge, which silently misordered batches with
  non-chronologically-sorting filenames and never reported missing/
  overlapping periods. A single-file batch (the dominant real-world case —
  e.g. a full-year statement for BoB/ICICI/HDFC/Kotak) is a no-op through
  `consolidate()`, so single-statement behavior for all banks is unchanged
  and verified byte-identical against the existing golden suite. Scope note:
  only the registry-driven `BankSkill.parse()` path (used by the GnuCash
  pipeline and covered by goldens) was changed for BoB/ICICI; each bank's
  legacy standalone-UI-tab `run()` entry point (used only by
  `ui/tabs/_generic.py`, untested by any golden) still does its old naive
  filename-sorted concat and was deliberately left untouched. HDFC and Kotak
  remain single-statement only (no multi-file path added). Central verdict
  engine, HDFC/Kotak, and the statement-profile engine untouched.

## [2.10.0] — 2026-07-18

### Added
- **Bank abstraction, P2 — HSBC (`skill_hsbc`, v1.1.0) onto the contract, and
  last bank of P2.** HSBC is the OCR bank (scanned PDFs -> Tesseract ->
  `parse_tsv.py` -> `enrich.py` -> `build_xlsx.py`), so unlike HDFC/BoB/ICICI
  its `BankSkill` boundary is a hybrid: `parse(path, password=None,
  output_path=None)` accepts a PDF, a folder of PDFs, or an already-enriched
  `.xlsx` (the existing `skill_gnucash_pipeline` call site passes an enriched
  workbook plus `output_path` and keeps working unmodified). A PDF/folder
  input runs the OCR pipeline end-to-end via a new `_run_ocr_pipeline()`
  helper (`--password` now threads through `run_pipeline.py` ->
  `ocr_to_tsv.py` -> `pdftoppm -upw`); an `.xlsx` input skips straight to
  `_read_enriched_rows()`. `BankStatementMeta.fidelity` is always
  `"ocr-approx"` (Tesseract output is inherently non-deterministic, never
  `"exact"`); `source_format` is `"pdf"`/`"pw-pdf"`. `_parse_number_hsbc` now
  delegates comma/Cr-Dr cleanup to `bank_common.normalize.clean_amount`
  instead of a private regex. Fixed a real data-loss bug:
  `_read_enriched_rows` was silently dropping the enriched workbook's "Extra
  Information" column; it's now folded into `Description`
  (`"<desc> | <extra>"`) so no field is lost. `detect()` gains an `.xlsx`/
  `.xlsm` fast path (checks for `Transaction Details`/`Withdrawals` headers,
  confidence 0.9) alongside the existing `.pdf` heuristic (text-layer sniff
  for "hsbc", confidence 0.8/0.5, 0.0 for a missing file). Registered in the
  `banks.py` registry via `skill.yaml` (`bank: true`, `bank_key: "hsbc"`)
  plus a module-level `bank_skill` instance; two false claims in its
  `help:` block (a stale "start Ollama" LLM troubleshooting entry and a
  false "text-extractable pages skip OCR" claim) corrected to match the
  actual direct-mode, always-OCR behavior. Session A's float-string
  OCR-confidence crash fix, direct/no-LLM mode, and multi-statement
  date-ordering + continuity detection were confirmed present in code (not
  assumed) and are untouched. Golden strategy differs from BoB/ICICI: OCR
  output isn't byte-deterministic, so the new golden family
  (`tests/skill_hsbc/hsbc_fixture_gen.py`) fixes the deterministic stage only
  — an already-enriched synthetic workbook, shaped exactly like
  `build_xlsx.py`'s real output, asserted against expected canonical rows,
  balances, and meta fields — while the existing OCR-stage tests
  (`parse_tsv.py`'s float-confidence + continuity tests) stay separate and
  untouched. No real HSBC corpus was available locally this session, so the
  pre-existing skipif-guarded corpus tie-out test skips cleanly rather than
  running. HDFC/BoB/ICICI not touched. This completes P2 (all four banks now
  on `BankSkill`); P3 (migrating the pipeline/UI to consume only the
  contract) is next.
- **Bank abstraction, P3a — contract-only pipeline.** `skill_gnucash_pipeline`
  now dispatches every dedicated bank (ICICI, Bank of Baroda, HSBC, HDFC)
  through a single registry-driven path — `agents.banks.discover()` matched
  on `display_name` → `load_bank_skill()` → `skill.parse(path,
  password=pdf_password)` — replacing the four hardcoded `if bank == "..."`
  branches and their four duplicated error-handling blocks with one. Restores
  the `BankSkill` protocol's actual contract (`parse(path, password=None) ->
  BankResult`, canonical rows only): removed the `output_path` side-channel
  from `BoBSkill.parse`, `ICICISkill.parse`, and `HSBCSkill.parse` — three of
  four banks had grown it to write their own canonical CSV, exactly the
  duplication `bank_contract.py`'s docstring forbids. The pipeline now writes
  the canonical CSV + sidecar exactly once, for every bank, via the shared
  `canonical_io.write_canonical_csv`/`write_sidecar` tail (previously only
  used inside each bank's own `parse()`). HDFC — the reference bank — is now
  called through `HDFCBankSkill.parse()` instead of `skill_hdfc.agent.run()`;
  `run()` itself is untouched and still backs the standalone HDFC UI tab.
  HSBC's two-step pipeline seam (`skill_hsbc.tools.run_hsbc_pipeline` to build
  an enriched `.xlsx`, then a separate `HSBCSkill().parse()` call) collapses
  to one: the pipeline now hands HSBC's PDF directory straight to
  `HSBCSkill.parse()`, which already folds OCR-to-enriched-workbook and
  enriched-to-canonical into a single call (from P2) — the
  `run_hsbc_pipeline` import is gone from the pipeline entirely. Each bank
  still needs a little input shaping before the uniform `parse()` call (ICICI
  and HDFC resolve a staged-upload directory to a single matching file; HSBC
  resolves to a PDF directory; BoB keeps its pre-existing "no PDFs found in
  this directory" check) — that's the one piece of per-bank logic that
  couldn't be pushed into the registry itself, since no two banks accept a
  staged upload in quite the same shape. This is a pure wiring phase — zero
  extraction/OCR/canonical-output behavior change, confirmed by every
  existing bank's golden tests passing unchanged (BoB/ICICI/HSBC-deterministic-
  stage/HDFC cross-format goldens all byte-identical). New tests: a contract-
  conformance check (`inspect.signature` asserts every discovered bank's
  `parse()` is exactly `(path, password=None)`, so the `output_path` drift
  can't silently return) and a registry round-trip test per bank (BoB/ICICI/
  HSBC — HDFC's already existed) that parses a synthetic fixture through
  `agents.banks` only, with no direct `from skill_* import`. Grep-verified
  zero `from skill_hdfc`/`skill_bob`/`skill_icici`/`skill_hsbc` imports remain
  in `skill_gnucash_pipeline/agent.py`. Central verdict engine and
  multi-statement-consolidation promotion into `bank_common` are deferred to
  P3b; `skill_itr_workbook`, `skill_26as`, and intercompany skills untouched.
- **Kotak Mahindra Bank onboarded as the 5th bank (`skill_kotak`).** New
  `src/agents/skill_kotak/` implements the `BankSkill` protocol
  (`detect`/`parse`/`formats`) for Kotak's ruled-table PDF statements:
  7 columns with separate Withdrawal (Dr.)/Deposit (Cr.) columns, `DD Mon
  YYYY` dates, Indian-grouped amounts, an "Opening Balance" pseudo-row
  excluded from canonical rows (mirroring BoB), multi-page overflow with no
  repeated header, and a trailing abbreviation-legend page rejected purely
  by column count (2 vs. 7) rather than a keyword blocklist. Sweep transfers
  to and from a linked FD are kept as real transactions. Adds a fully
  synthetic fixture family (`tests/skill_kotak/kotak_fixture_gen.py`)
  covering the golden path, legend exclusion, multi-page continuation,
  password-protected PDFs, and garbled-text rejection, plus registry
  round-trip and `discover() == 5` coverage in `tests/test_banks_registry.py`.
  The only pipeline edit is declarative: "Kotak" added to the bank dropdown
  and help text in `skill_gnucash_pipeline/skill.yaml`. (PR #89, `8c85f4e`.)

### Fixed
- **Bank gating, registry-driven (closes the Kotak offer-then-reject leak).**
  Onboarding Kotak (#89) added it to the `skill_gnucash_pipeline` Bank
  dropdown's static `options:` list but not to `agent.py`'s hardcoded
  `DEDICATED_BANKS = ["ICICI", "Bank of Baroda", "HSBC", "HDFC"]`, so
  selecting "Kotak" in the UI passed the dropdown but then failed the
  `SUPPORTED_BANKS` guard at runtime ("Supported banks: ICICI, Bank of
  Baroda, HSBC, HDFC") — an offer-then-reject bug live on `main`. Both
  gating surfaces are now registry-driven off the single source of truth,
  `agents.banks.discover()`, so they can never diverge again for any future
  bank: a new `_options_from_banks()` resolver in `ui/tabs/_generic.py`
  (registered as `"banks"` in `_OPTIONS_FROM_RESOLVERS`) drives the dropdown
  via `skill_gnucash_pipeline/skill.yaml`'s `bank` input (now
  `options_from: "banks"` instead of a static list), and `DEDICATED_BANKS`
  is now `[b.display_name for b in discover()]` instead of a literal. Dropdown
  order is now alphabetical by `display_name` with "Other Bank (CSV)" last
  (an accepted cosmetic change — previously ICICI/BoB/HSBC/HDFC/Kotak/Other).
  No dispatch logic changed; all 5 banks' extraction goldens remain
  byte-identical. New `tests/test_bank_gating.py` is the permanent regression
  guard: asserts `"Kotak" in SUPPORTED_BANKS`, `DEDICATED_BANKS == [b.display_name
  for b in discover()]`, and that the dropdown's resolved options exactly
  match `discover()` display names + `"Other Bank (CSV)"` last.

## [2.9.0] — 2026-07-17

### Added
- **ITR Mapping review UI polish (Part 4): sortable/filterable columns.**
  The "ITR Mapping" table's headers are now click-to-sort (click again to
  flip direction) with a per-column text filter row underneath — the same
  UX as `ui/tabs/gnucash_review.py`'s "Review & Edit Account Mappings" tab,
  which this screen had not previously matched.
- **ITR Mapping review UI polish (Part 4): tag vocabulary help.** Every tag
  code shown in the table (Current tag / Suggested / New tag) now carries a
  hover tooltip with its one-line meaning, and a new toggleable "? Tag
  glossary" panel lists the full, searchable tag vocabulary (code, target
  sheet, meaning) — the raw tag codes (e.g. `OS_INTEREST_BANK`) previously
  had no in-UI explanation.
- **Bank abstraction, P2 — ICICI (`skill_icici`, v1.1.0) onto the contract.**
  `ICICISkill.parse()` now returns a fully populated `BankStatementMeta` —
  account number and statement period parsed from the XLS "Search" preamble
  (`Account Number` / `Transaction Date from ... to ...` rows), `source_format`
  (`"xls"`), `fidelity`, and `password_used` (ICICI statements are never
  password-protected). ICICI now builds on `bank_common.normalize` for amount
  cleanup and date parsing instead of a private `MONTH_MAP`: a new
  `parse_comma_month_date()` / `MONTH_ABBR` pair handles ICICI's distinctive
  "DD,Mon,YYYY" date shape, layered under the same `clean_amount()` used by
  HDFC/BoB. `formats()`/`detect()`/the directory glob in `parse()` are
  narrowed from `(".xls", ".xlsx")` to `(".xls",)` — a pre-existing latent
  bug, since `xlrd` 2.x cannot actually read `.xlsx` (support dropped in
  2.0+), and ICICI's own `skill.yaml` already declared `.xls`-only. Registered
  in the `banks.py` registry via `skill.yaml` (`bank: true`, `bank_key:
  "icici"`) plus a module-level `bank_skill` instance. New synthetic golden
  fixture (`tests/skill_icici/icici_fixture_gen.py`): a single `.xls` (ICICI
  has only one real input shape) encoding 5 fake transactions in the real
  12-row-preamble + header-row-13 + data-row-14+ layout, with an identity
  test asserting the expected canonical rows, balances, and meta fields, plus
  a `.xlsx`-rejected test. Canonical CSV output verified byte-identical
  before/after migration against the real local ICICI corpus sample (465
  rows, opening/closing balances unchanged). HDFC/BoB/HSBC not touched.

### Pending
- **Frozen-build UI smoke test** (Harshal-side, PortableApps install) not
  run as part of this release — flagged pending, not blocking.

## [2.8.0] — 2026-07-16

### Added
- **Bank abstraction, P2 — BoB (`skill_bob`, v1.1.0) onto the contract.**
  `BoBSkill.parse()` now accepts an optional `password` and returns a fully
  populated `BankStatementMeta` — account number and statement period parsed
  from the PDF front matter (`A/C Number :` / `Statement of account for the
  period of ...`), `source_format` (`pdf` / `pw-pdf`), `fidelity`, and
  `password_used` (never the password). BoB now builds on `bank_common`
  instead of private duplicates: `normalize.clean_amount`/`normalise_date`
  (extended with a trailing Cr/Dr balance-suffix strip and dash-separated
  dates), `text_quality.text_layer_usable` (rejects a garbled/scanned text
  layer before parsing, no OCR fallback), and `password.is_password_error`
  (clear, non-echoing password-error messages). `extract_bob_statement.py`'s
  page-1 x-coordinate column-geometry detection (multi-page tables without a
  repeated header row) stays BoB-specific but now sits on top of these shared
  primitives. Registered in the `banks.py` registry via `skill.yaml`
  (`bank: true`, `bank_key: "bob"`). New synthetic cross-format golden family
  (`tests/skill_bob/bob_fixture_gen.py`): the same 5 fake transactions as a
  2-page PDF (no repeated header, Cr-suffixed balances) and the native CSV
  `extract_bob_statement.py` emits, with an identity test asserting
  byte-identical canonical rows. Canonical CSV output verified byte-identical
  before/after migration against the real local BoB corpus sample (74 rows,
  opening/closing balances unchanged) and the Session-A independent
  closing-balance verdict fix is untouched. HDFC/ICICI/HSBC not touched.

## [2.7.0] — 2026-07-16

### Added
- **ITR Mapping review UI polish (Part 3): RAG confidence coding.** Mapped
  accounts on the "ITR Mapping" tab now show a confidence tier instead of
  just a tag: green "(confirmed)" once a human has approved/set the entry,
  amber "(needs review)" while it's still an unapproved LLM suggestion
  (`suggested_by_llm` set), red "UNMAPPED" as before — shown as both a
  left-border row accent and an inline badge. The "Show" filter gained
  "Needs review" and "Confirmed only" options alongside the existing
  All/Unmapped/Mapped.
- **Bank abstraction, P1 — contracts.** `agents/bank_contract.py` gains
  `BankStatementMeta` (account number, statement period, source format,
  OCR-vs-exact fidelity, password-used flag — never the password itself) and
  `RowProvenance`; `BankResult` now carries an optional `meta` field, and the
  `BankSkill` protocol gains `formats()`. New `agents/bank_common/` package
  (`normalize`, `tabular`, `text_quality`, `password`) promotes HDFC's
  header-detection, alias-table mapping, date/amount normalization, garbled-
  PDF-text-layer heuristic, and password-error handling into shared,
  bank-agnostic utilities — moved verbatim, so behavior is unchanged. HDFC
  (`skill_hdfc`) is re-expressed on `BankSkill` (`detect()`/`parse()`/
  `formats()` + a `bank_skill` instance) alongside its existing `run()` entry
  point, which is untouched; `parse()` shares the same extraction core via a
  new `_extract_transactions()` helper, verified byte-identical against the
  existing cross-format golden suite. New `agents/banks.py` registry
  discovers banks via a `bank: true` skill.yaml key (frozen-safe, no dynamic
  imports at discovery time — same pattern as `agents/registry.py`); HDFC is
  the first bank onboarded to it. BoB/HSBC/ICICI (already on `BankSkill` from
  earlier work) needed a matching `formats()` method added to stay conformant
  with the extended protocol — no change to their parsing logic. Pipeline
  dropdown/Banks-tab wiring to the new registry and migrating BoB/HSBC/ICICI
  onto it are deferred to later sessions (one bank per session; the pipeline
  already dispatches to their existing `BankSkill` classes directly).

### Fixed
- **ITR Mapping "Show" filter defaulted to hiding everything but unmapped
  rows**, and its native `<select>` chrome had poor contrast against the
  dark theme (reported as barely visible). The filter now defaults to
  **All** and is explicitly styled to match the rest of the tab.

### Changed
- **ITR nav restructured.** "ITR Workbook" and "ITR Mapping" were flat
  sub-tabs directly under GnuCash; they now live inside a single "ITR"
  sub-tab (GnuCash > ITR > ITR Workbook / ITR Mapping), mirroring how
  "Banks" already groups its own sub-tabs.

### Pending
- **Frozen-build UI smoke test** (Harshal-side, PortableApps install) not
  run as part of this release — flagged pending, not blocking.

## [2.6.0] — 2026-07-16

### Added
- **ITR Mapping review UI (Part 2).** A new "ITR Mapping" tab (GnuCash >
  ITR Mapping, next to ITR Workbook) gives the account-tag mapping the same
  review UX as the post-bank-transformation "Review & Edit Account Mappings"
  tab — no more hand-editing the `-proposed-mappings.yaml` snippet or
  running a CLI script:
  - Select an entity (same `Data/itr/entities.yaml` dropdown source as the
    ITR Workbook tab); Load shows every account for that entity, sourced
    from `Data/itr/mappings/<entity>.mapping.yaml` plus the most recent
    `-proposed-mappings.yaml` run artifact — unmapped accounts are flagged
    (red UNMAPPED badge) with any LLM suggestion shown alongside.
  - A searchable tag-assignment picker (typeahead over `tags.py`'s
    vocabulary, showing each tag's description) plus row multi-select and
    "Apply to selected", mirroring `ui/tabs/gnucash_review.py`'s account
    picker.
  - Save writes `Data/itr/mappings/<entity>.mapping.yaml` (anchored via
    `data_root_dir()`, works in both source and frozen layouts) —  always
    backing up the pre-save file first (timestamped `.bak-YYYYMMDD-HHMMSS`)
    before any in-place rewrite, and never touching disk at all for a blank
    entity or an empty change set. Touched entries are marked approved
    (`suggested_by_llm` cleared, note replaced) the same way the CLI
    correction script already did.
  - `apply_mapping_corrections.py` gained an importable
    `apply_corrections_map(mapping_file, {guid: tag}, output_yaml, paths=...)`
    — the new core the UI calls directly (no more shelling out); the
    existing CLI (`apply_corrections(mapping_file, reviewed_xlsx,
    output_yaml)`) is now a thin wrapper over it and its behaviour is
    unchanged (round-trip test still green).

### Pending
- **Frozen-build UI smoke test** (Harshal-side, PortableApps install) not
  run as part of this release — flagged pending, not blocking.

## [2.5.0] — 2026-07-16

### Changed
- **ITR Workbook — best-effort workbook instead of block-to-nothing (Part
  1).** An unmapped account used to set `STATUS: BLOCKED-FOR-REVIEW` and
  skip the workbook build entirely (`_build_and_write_workbook` returned
  `[]`, and `run()` wrote a one-sheet scaffold) — a user with even one
  unmapped leaf got nothing usable. Any unmapped leaf (a partially mapped
  file, or a true cold start with none) now still builds the full
  BS + P&L + IT working workbook:
  - Every unmapped leaf routes into a new UNCLASSIFIED/REVIEW bucket
    (`schedules.build_unclassified`, rendered on a new `Unclassified` sheet
    plus red call-outs on Mapping Review/Reconciliation) instead of being
    silently dropped — its amount is included in that bucket's own total,
    so the accounting identity (Assets = Equity+Liabilities; the
    RetainedEarnings P&L control total) still ties out exactly.
  - The IT working (Computation sheet) shows two tax figures whenever
    anything is unclassified: **DRAFT** (tax computed on resolved items
    only, stamped with the unclassified count/₹ total, not filing-ready)
    and a **worst-case upper bound** (every unclassified INCOME-type leaf
    assumed fully taxable at the top slab rate for the selected regime;
    unclassified expense/deduction/BS-side items are never assumed to
    reduce tax — conservative). Neither is presented as a final total; the
    plain "Tax liability" row is relabelled DRAFT whenever N > 0.
  - `STATUS: BLOCKED-FOR-REVIEW` (nothing built) is replaced by
    `STATUS: BUILT -- N REVIEW ITEM(S)` whenever N > 0; the
    `<output>-proposed-mappings.yaml` learning-loop snippet is still
    written every time.
  - Hard-error paths (unparseable HTML, unresolved entity, AY-vs-HTML
    mismatch, a mapping file with a `VALIDATION ERROR`) are unchanged —
    still fail loud with a stub, no workbook.
  - A fully-mapped run (0 unmapped) is unchanged: no DRAFT stamp, no
    `Unclassified` sheet, tax shown as final — same as before this change.
  - Part 2 (an in-app ITR Mapping review UI, so a user never has to
    hand-edit the proposed-mappings YAML) is tracked separately and not
    included in this release.

### Pending
- **Frozen-build smoke test** (Harshal-side, PortableApps install) not run
  as part of this release — flagged pending, not blocking.

## [2.4.0] — 2026-07-16

### Fixed
- **ITR Workbook — Data/itr paths no longer double up under the frozen
  Launcher.** `entities_path`/`rules_dir`/`scrips_path` were CWD-relative
  defaults with `Data/` baked in (`agent.py::run()`); the frozen PortableApps
  build sets CWD to `...\Data\`, so they silently resolved to
  `...\Data\Data\itr\...` and the run read stale/empty config while the
  entity/AY dropdowns (already anchored via `ui/_config.data_root_dir()`)
  showed the correct list. `ui/tabs/_generic.py` now anchors all three via a
  new `{data_root}` `run_args` token (same anchor the dropdowns use);
  `skill.yaml`'s `run_args` route through it. Agent defaults remain a
  source-mode-only fallback.
- **ITR Workbook — missing/unresolved entity now fails loud.** A missing or
  unreadable `entities.yaml`, or an explicitly selected entity not found in
  it, used to silently substitute a generic `UNKNOWN`/`Individual`/new-regime
  profile — picking the wrong regime/age band without any warning.
  `agent.py::_resolve_entity()` now raises when an *explicitly selected*
  entity can't be resolved, naming the resolved path it looked at; the run
  reports an `ERROR:` summary and writes no green stub. An entity key merely
  *inferred* from a mapping file's stem (no explicit selection) still
  degrades gracefully, unchanged.
- **ITR Workbook — mapping-less run no longer silently emits an empty green
  stub.** With the Entity mapping box empty, a run used to report
  `STATUS: OK` and write a one-sheet scaffold with no schedules — easy to
  mistake for a real, populated workbook. Two changes: (1) when an entity is
  selected and it has an existing
  `<data_root>/itr/mappings/<entity>.mapping.yaml`, the run now auto-derives
  and uses it (logged in the summary as `Mapping: auto-derived ...`); (2) a
  true cold start (no mapping anywhere for the entity) now treats every leaf
  as unmapped and routes into the existing BLOCKED-FOR-REVIEW +
  proposed-mappings-snippet learning loop, the same as a partially mapped
  file — a mapping-less run can no longer report a green `STATUS: OK`.

## [1.0.1] — 2026-06-25

### Fixed
- **README skills table** — was stale at 9 skills from the 1.0.0 release;
  now lists all 16 user-facing skills (KRChoksey ledger/import/reconcile,
  HDFC, GnuCash Import pipeline, 26AS Journal, ICICI added since), grouped
  to match the UI, with accurate mode and LLM-requirement columns.

### Verified
- Frozen-build smoke test against current `main` (sha `9158ed5`): rebuilt
  `pa_skills.exe` from source, launched it, confirmed HTTP 200 on the
  Gradio root and all 16 skill tabs present in the served UI tree. No
  regressions found.

## [1.0.0] — 2026-06-04

### Added
- **MSG / Email Parser skill** — direct-mode skill that parses `.msg`
  (Outlook) and `.eml` files into structured JSON (sender, date, subject,
  body, attachment list). Uses `extract-msg` for `.msg` and stdlib `email`
  for `.eml`. No LLM required. (B7 resolved)
- **Auto-update checker** — Home tab checks the GitHub releases API on
  startup and shows a banner when a newer version is available. Background
  thread, cached for the process lifetime. (D2 resolved)
- **Frozen-build CI smoke test** — new step in `release.yml`: launches
  `pa_skills.exe`, waits for port file, GETs the root URL, verifies
  HTTP 200, then kills the process. Catches PyInstaller bundling
  regressions. (E4 resolved)
- **qpdf vendored** — added to `binaries.toml`, `refresh_binaries.py`,
  `build.py` (step 6b), and `_native.py` resolver. CC Sort no longer
  requires qpdf to be installed by the user. (B2 resolved)
- Unit tests for `_config.py` (~15 tests), `_runner.py` (~12 tests),
  `_health.py` (~13 tests), MSG parser (~15 tests), update checker
  (~12 tests). Total test count ~175+. (E1 substantially resolved)
- `README.md` overhauled — architecture overview, dev + user setup,
  skill authoring guide, CI badge. (F1 resolved)
- `BUILDING.md` — consolidated build guide replacing 4 date-stamped
  notes files. (F3 resolved)

- **Dependency management infrastructure:**
  - `requirements-lock.txt` — exact pinned versions for reproducible
    frozen builds. `build.py` prefers the lock file when present.
  - Dependabot config (`.github/dependabot.yml`) — weekly PRs for pip
    and GitHub Actions dependency updates.
  - Native binary update checker (`.github/workflows/check-native-binaries.yml`)
    — weekly scheduled job checks Tesseract, Poppler, qpdf releases via
    GitHub API and opens an issue when updates are available.
  - Compatibility check (`.github/workflows/compat-check.yml`) — weekly
    scheduled job installs latest-compatible deps from loose pins, runs
    full test suite, and attempts frozen build + smoke test.

### Changed
- **Upstream repo published** — `platform-agnostic-skills` pushed to
  GitHub. `sources.toml` switched from `kind = "local"` to `kind = "git"`
  with public URL. CI no longer uses `--skip-pull`. (B6 resolved)
- Historical plan/notes/prompt files moved to `docs/history/`.
- Skill count: 8 → 9 (MSG Parser added).
- `_native.py` now resolves Tesseract, Poppler, and qpdf.
- `.gitattributes` comment updated for qpdf.

## [0.4.1] — 2026-06-03

### Fixed
- **Self-hosted Launcher Generator (D1)** — bundled PortableApps.com
  Launcher Generator v2.2.4 under `bundling/launcher-gen/2.2.4/` so CI
  no longer needs to download it from portableapps.com (TLS handshake
  was failing on GitHub-hosted runners). Every release zip now ships
  with `PASkillsPortable.exe`.
- Simplified `release.yml` — removed download step, `SKIP_LAUNCHER`
  env var, and fallback zip logic.
- Updated `build.py` `LAUNCHER_GEN_HINTS` to check bundled copy first.
- Smoke test `test_registry_discovers_all_skills` updated for 8 skills.
- `test_webui_constructs` skipped gracefully when gradio is not installed.
- `test_history_tab.py` mocks gradio safely (try real import first).

## [0.4.0] — 2026-06-03

### Added
- `type: "select"` input for generic skill tabs — renders a `gr.Dropdown`
  with predefined choices from `options:` in `skill.yaml`. Allows custom
  values typed by the user.
- `--clean` flag on `bundling/build.py` — deletes
  `build_pyinstaller/.agents_cache/` and exits.
- **Agent progress streaming (C4)** — agent-mode skills now show live
  intermediate steps (tool calls, tool results, LLM reasoning) in the
  result area instead of just elapsed-time ticks. Implemented via
  `_StreamingAgentWrapper` in `base_agent.py` and `run_with_streaming()`
  in `_runner.py`. Zero changes to individual skill files.
- **Skill output history tab (C5)** — scan outputs directory, sortable
  table with download and delete actions.
- **Document Summarizer** — direct-mode skill for PDF/text summarization.
- **Text Translator** — direct-mode skill with select dropdowns.
- **CSV Data Analyzer** — agent-mode skill with pandas tools and safety guards.
- 60-test suite for Phase 4C skills with synthetic fixtures.
- 30-test suite for history tab.
- 17 unit tests for streaming infrastructure.
- End-to-end test runner (`test_4c_e2e.bat`) for Ollama-backed validation.

### Changed
- CI Python version bumped from 3.10 to 3.13 to match `pyproject.toml`
  (`requires-python = ">=3.13"`).
- Translator skill now uses `type: "select"` dropdowns for source/target
  language instead of free-text fields.

---

## Phase 4C — 2026-05-28

### Added
- **Document Summarizer** skill (`skill_summarize`, `mode: "direct"`) —
  upload a PDF or text file, get a structured markdown summary with
  Key Points / Detailed Summary / Conclusions sections.
- **Text Translator** skill (`skill_translate`, `mode: "direct"`) — paste
  text, specify source and target languages, get a translated `.txt` file.
  Works with any chat-capable LLM including local models via Ollama.
- **CSV Data Analyzer** skill (`skill_csv_analyzer`, `mode: "agent"`) —
  upload a CSV file and ask a natural-language question. Uses pandas-based
  tools (`describe_csv`, `query_csv`) with expression safety guards
  (allowlist + blocklist) to prevent code injection.
- 60 unit tests in `tests/test_phase4c_skills.py` covering registry
  discovery, YAML validation, file reading, truncation, input validation,
  safety guards, and CSV tool functions with synthetic fixtures.

---

## Phase 4B — 2026-05-27

### Added
- Multi-file upload input type (`type: "files"` in `skill.yaml`) — Gradio
  `file_count="multiple"`, staged into a temp directory for the skill.
- BoB skill updated to accept multiple PDF uploads via `type: "files"`.

### Changed
- HSBC skill switched from `type: "file"` to `type: "directory"` input
  to match its agent API (accepts a folder of PDFs).
- cc_sort and cc_transactions agent scripts: replaced `subprocess` calls
  with `runpy.run_path()` for frozen-mode compatibility.
- Fixed `check_extract_msg_available` — was broken in frozen mode due to
  `sys.executable -c` pattern; now uses direct import.

### Fixed
- Frozen-mode subprocess failures for cc_sort and cc_transactions skills.

---

## Phase 4A — 2026-05-27

### Added
- **Pluggable skill architecture:** `agents/registry.py` auto-discovers
  `agents/*/skill.yaml` at startup and exposes `SkillInfo` dataclass
  objects. Adding a new skill requires only a `skill.yaml` manifest —
  no code changes to `webui.py`.
- **Generic tab rendering:** `ui/tabs/_generic.py` dynamically builds
  Gradio tabs from skill manifests. Supports `file`, `directory`, and
  `text` input types.
- **`run_direct()` execution path** in `base_agent.py` for simple
  prompt → LLM → response skills (no tools, no agent loop). Skills
  declare `mode: "direct"` in `skill.yaml` to use it.
- Home tab now dynamically lists all discovered skills from the registry
  instead of hard-coded text.

---

## Phase 3 — 2026-05-25 (v0.3.0 / v0.3.1)

### Added
- Real icon artwork (gear + sparkle, rounded container) at 16/32/75/128 px.
- Real `git clone --depth 1` agent pull from `sources.toml` with
  SHA-256-keyed cache at `build_pyinstaller/.agents_cache/`.
- GitHub Actions CI release pipeline (`.github/workflows/release.yml`)
  triggered on tag push. Graceful fallback when PortableApps.com Launcher
  Generator CDN is unreachable from Azure runners.
- Vendored Tesseract 5.4.0 and Poppler 24.07.0 binaries via Git LFS.

---

## Phase 2b — 2026-05-24 (v0.2.0)

### Added
- `step8_render_inis` — renders `appinfo.ini` and Launcher INI from
  `bundling/templates/`.
- `step9_copy_defaults` — copies `bundling/templates/DefaultData` into
  staging.
- `step10_launcher_gen` — invokes the PortableApps.com Launcher Generator
  to produce `PASkillsPortable.exe`.
- `step11_zip` — builds a deterministic `dist/PASkillsPortable_<ver>.zip`.
- CLI flags: `--launcher-gen <PATH>`, `--skip-launcher`.

### Changed
- `paskills.spec`: `console=True` → `console=False`. `pa_skills.exe` now
  runs windowless; the PortableApps launcher is the user-facing entry point.

---

## Phase 2a — 2026-05-22

### Added
- Tesseract + Poppler vendoring under `vendor/` via Git LFS.
- `bundling/refresh_binaries.py` — download + SHA-256 verify + extract
  native binaries into `vendor/`.
- `ui/_native.py` — resolves native binary paths, prepends to `PATH`,
  configures `pytesseract`. Idempotent.
- BoB tab (`ui/tabs/skill_bob.py`) — pdfplumber only, no native binaries.
- HSBC tab (`ui/tabs/skill_hsbc.py`) — calls `_native.ensure_native_path()`
  with clear UI error if Tesseract or Poppler are missing.
- `build.py` steps 5–6: copy `vendor/*` into `staging/App/PASkills/`.
- `build.py` step 7: reads `bundling/sources.toml` for agent pull source
  (local sibling folder or git clone).
- Three new smoke tests: BoB import, HSBC import, `_native` resolver.

### Changed
- Pinned `gradio>=6.0,<7.0` in `requirements.txt` and `pyproject.toml`.
- Fixed `datetime.utcnow()` → `datetime.now(timezone.utc)` in `build.py`.

---

## Phase 1 — 2026-05-20

### Added
- Phase 1 scaffold of the portable packaging project per the v0.2 spec
  (`2026-05-01-portable-apps-packaging-spec.docx`).
- Repo root: `LICENSE` (Apache 2.0), `NOTICE`, `README.md`, `pyproject.toml`,
  `requirements.txt`, `.gitignore`, `.gitattributes` (LFS rules per spec §10.5).
- Source tree skeleton: `src/agents/`, `ui/{tabs,_buildinfo.py}`,
  `bundling/{templates,icons}`, `tests/`, placeholder `vendor/`.
- `src/agents/` mirrored from the sibling `platform-agnostic-skills` project
  per the build-time-pull contract (locked decision §15.1).
- Minimal Gradio `ui/webui.py` with Home and 26AS tabs only, custom black +
  electric-blue theme (locked decision §15.4), bound to 127.0.0.1 with
  free-port pick.
- `bundling/build.py` covering steps 1–4 of spec §10.2 (read git tag,
  reset staging, create venv, run PyInstaller `--onedir`).
- `bundling/paskills.spec` with hidden imports per spec §10.3.

### Changed
- Renamed top-level `packaging/` folder to `bundling/` to avoid a Python
  import-path shadow on the PyPI `packaging` package.

### Notes
- Native binaries (Tesseract, Poppler) deferred to Phase 2.
- AppInfo, DefaultData, Launcher INI, and Launcher Generator deferred to Phase 2.
- Frozen `pa_skills.exe` must be smoke-tested manually; see `BUILD-NOTES.md`.
