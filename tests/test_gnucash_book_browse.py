"""
tests/test_gnucash_book_browse.py -- "Browse..." wiring for the GnuCash book
picker on the three hand-written review tabs (Phase 0a).

A bare gr.File in the browser stages a *temp copy* of whatever the user
selects and hands the run handler that temp path -- catastrophic for a live
.gnucash (wrong path, breaks the book GUID, forces re-prompting every run).
Each of these tabs now sits a "Browse..." button next to the gr.File that
opens the native OS picker (ui/_filedialog.pick_files) and sets the File's
value to the REAL absolute path server-side, bypassing the browser's
upload-copy. This mirrors ui/tabs/_generic.py's own "Browse..." wiring (see
the `for _brbtn, _fcomp, _inp, _multiple in browse_buttons:` block there).

We don't stand up a full Gradio server or click through a browser -- that
would add heavy scaffolding for little value over what ui/_filedialog.py's
own tests already cover (folder memory, extension/size validation, the
native dialog boundary). Instead we render each tab inside a real
`gr.Blocks()` context (proving it renders without error), capture the exact
handler function registered on the "Browse..." button's `.click(...)`, and
call that handler directly with `_filedialog.pick_files` monkeypatched --
exercising exactly the logic Phase 0a added: forwarding valid picks into
`gr.update(value=...)`, keeping the current value on cancel/reject, and
surfacing warnings via `gr.Warning`.

Run with:
    cd "<repo>" && python -m pytest tests/test_gnucash_book_browse.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import gradio as gr
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _capture_browse_click(tmp_path: Path, render_module_path: str, render_kwargs=None):
    """Render `<render_module_path>.render()` inside a gr.Blocks() context with
    output_dir() pointed at an empty tmp_path (so no real Data/outputs dir is
    ever touched), and return the exact fn registered on the "Browse..."
    button's .click(...).
    """
    import importlib

    module = importlib.import_module(render_module_path)

    captured: dict = {}
    orig_click = gr.Button.click

    def _tracking_click(self, fn=None, **kwargs):
        if self.value == "Browse...":
            captured["fn"] = fn
        return orig_click(self, fn=fn, **kwargs)

    with patch.object(gr.Button, "click", _tracking_click):
        with patch.object(module._config_mod, "output_dir", return_value=tmp_path):
            with gr.Blocks():
                module.render(**(render_kwargs or {}))

    assert "fn" in captured, (
        f"{render_module_path}.render() did not register a .click() handler "
        "on a Button labelled 'Browse...'"
    )
    return module, captured["fn"]


MODULES = [
    "ui.tabs.gnucash_review",
    "ui.tabs.tds_journal_review",
    "ui.tabs.krc_gnucash_review",
]


@pytest.mark.parametrize("module_path", MODULES)
def test_browse_button_renders_and_is_wired(module_path, tmp_path):
    """Every tab renders a "Browse..." button next to the GnuCash gr.File and
    wires its click to a handler (proves the button + wiring exist at all)."""
    _capture_browse_click(tmp_path, module_path)


@pytest.mark.parametrize("module_path", MODULES)
def test_browse_handler_forwards_valid_pick_as_real_path(module_path, tmp_path):
    """A successful native pick must set the File box's value to the picked
    REAL path via gr.update(value=...) -- never a browser temp-upload path."""
    module, fn = _capture_browse_click(tmp_path, module_path)

    fake_path = str(tmp_path / "MyBook.gnucash")
    with patch.object(module._filedialog, "pick_files", return_value=([fake_path], [])):
        result = fn()

    assert result == {"value": fake_path, "__type__": "update"}


@pytest.mark.parametrize("module_path", MODULES)
def test_browse_handler_keeps_current_value_on_cancel(module_path, tmp_path):
    """Cancelling the native dialog (or every pick being rejected) must leave
    the File box's current value alone -- not clear it."""
    module, fn = _capture_browse_click(tmp_path, module_path)

    with patch.object(module._filedialog, "pick_files", return_value=([], [])):
        result = fn()

    assert result == {"__type__": "update"}


@pytest.mark.parametrize("module_path", MODULES)
def test_browse_handler_surfaces_warnings(module_path, tmp_path, monkeypatch):
    """A rejected pick (wrong extension / oversize) comes back as a warning
    string from pick_files and must be surfaced via gr.Warning, not silently
    dropped."""
    module, fn = _capture_browse_click(tmp_path, module_path)

    seen_warnings = []
    monkeypatch.setattr(gr, "Warning", lambda msg: seen_warnings.append(msg))

    with patch.object(
        module._filedialog, "pick_files", return_value=([], ["Not a .gnucash file: foo.txt"])
    ):
        result = fn()

    assert seen_warnings == ["Not a .gnucash file: foo.txt"]
    assert result == {"__type__": "update"}


@pytest.mark.parametrize("module_path", MODULES)
def test_browse_uses_stable_per_tab_box_key(module_path, tmp_path):
    """The box_key passed to pick_files must be f"{APP_ID}.gnucash_book" --
    stable per tab (drives per-box folder memory) and distinct across tabs."""
    module, fn = _capture_browse_click(tmp_path, module_path)

    captured_kwargs = {}

    def _fake_pick_files(box_key, **kwargs):
        captured_kwargs["box_key"] = box_key
        captured_kwargs.update(kwargs)
        return ([], [])

    with patch.object(module._filedialog, "pick_files", _fake_pick_files):
        fn()

    assert captured_kwargs["box_key"] == f"{module.APP_ID}.gnucash_book"
    assert captured_kwargs["multiple"] is False
    assert captured_kwargs["file_types"] == (".gnucash",)
    assert captured_kwargs["max_size_bytes"] == module._MAX_UPLOAD_SIZE_BYTES


def test_box_keys_are_distinct_across_tabs():
    """Sanity check: the three tabs' box_keys must not collide (each keys its
    own remembered folder)."""
    import ui.tabs.gnucash_review as rv
    import ui.tabs.krc_gnucash_review as krc
    import ui.tabs.tds_journal_review as tdsjr

    keys = {f"{m.APP_ID}.gnucash_book" for m in (rv, tdsjr, krc)}
    assert len(keys) == 3
