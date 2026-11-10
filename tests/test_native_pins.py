"""
Native-binary pins, the fetch step placeholder handling, build.py early
check, and the upstream-version comparison.

Every guard has a test that asserts the WRONG behaviour does not happen
(nothing extracted, nothing skipped, build not allowed to continue).
"""
from __future__ import annotations

import importlib.util
import inspect
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
REAL_TOML = ROOT / "bundling" / "binaries.toml"
WORKFLOW = ROOT / ".github" / "workflows" / "check-native-binaries.yml"
PLACEHOLDER = "REPLACE_WITH_PINNED_SHA256_AT_PHASE_2"
GOOD_SHA = "a" * 64


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


rb = _load("pask_refresh_binaries", ROOT / "bundling" / "refresh_binaries.py")
bld = _load("pask_build", ROOT / "bundling" / "build.py")
nbu = _load("pask_native_binary_updates", ROOT / "scripts" / "native_binary_updates.py")

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib


# ---------------------------------------------------------------------------
# The real pins
# ---------------------------------------------------------------------------

def _real():
    return tomllib.loads(REAL_TOML.read_text(encoding="utf-8"))


def test_real_toml_has_no_placeholder_and_only_real_hashes():
    assert bld.check_binaries_toml(REAL_TOML) == []


def test_real_pins_are_the_agreed_ones():
    d = _real()
    t = d["tesseract"]
    assert t["version"] == "5.5.0.20241111"
    assert t["kind"] == "installer"
    assert t["url"].startswith("https://github.com/tesseract-ocr/tesseract/releases/download/5.5.0/")
    assert "UB-Mannheim" not in t["url"]
    assert d["tessdata"]["eng"]["url"].startswith("https://github.com/tesseract-ocr/tessdata_fast/")
    assert d["tessdata"]["eng"]["sha256"] == (
        "7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2")
    assert d["poppler"]["source_url"].endswith("poppler-24.07.0.tar.xz")
    assert re.fullmatch(r"[0-9a-f]{64}", d["poppler"]["source_sha256"])


# ---------------------------------------------------------------------------
# refresh_binaries: placeholder, wrong SHA, installer
# ---------------------------------------------------------------------------

@pytest.fixture
def fetch(monkeypatch):
    """A fake download plus a spy installer. `calls` records any extraction."""
    calls = []

    def fake_download(url, dest):
        Path(dest).write_bytes(b"payload")

    monkeypatch.setattr(rb, "_download", fake_download)
    monkeypatch.setitem(rb.INSTALLERS, "poppler", lambda meta, tmp: calls.append("poppler"))
    return calls


def _meta(sha):
    return {"version": "1", "url": "https://example.invalid/p.zip", "sha256": sha}


def test_placeholder_sha_raises_and_extracts_nothing(fetch, capsys):
    with pytest.raises(rb.PlaceholderShaError):
        rb.refresh_one("poppler", _meta(PLACEHOLDER))
    assert fetch == []                       # nothing was extracted
    out = capsys.readouterr().out
    assert "actual:" in out and "sha256 = " in out     # the real hash is printed


def test_wrong_sha_raises_and_extracts_nothing(fetch):
    with pytest.raises(RuntimeError, match="mismatch"):
        rb.refresh_one("poppler", _meta(GOOD_SHA))
    assert fetch == []


def test_correct_sha_does_extract(fetch, monkeypatch):
    """Positive control: the two tests above are not passing vacuously."""
    import hashlib
    sha = hashlib.sha256(b"payload").hexdigest()
    assert rb.refresh_one("poppler", _meta(sha)) is True
    assert fetch == ["poppler"]


def test_main_exits_2_on_placeholder_with_nothing_extracted(fetch, monkeypatch):
    monkeypatch.setattr(rb, "_load_binaries", lambda: {"poppler": _meta(PLACEHOLDER)})
    assert rb.main(["--target", "poppler"]) == 2
    assert fetch == []


def test_main_exits_2_on_wrong_sha(fetch, monkeypatch):
    monkeypatch.setattr(rb, "_load_binaries", lambda: {"poppler": _meta(GOOD_SHA)})
    assert rb.main(["--target", "poppler"]) == 2
    assert fetch == []


def test_installer_target_is_refused_without_downloading(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not download an installer")
    monkeypatch.setattr(rb, "_download", boom)
    meta = dict(_real()["tesseract"])
    with pytest.raises(rb.InstallerTargetError, match="--from-tesseract"):
        rb.refresh_one("tesseract", meta)
    assert rb.main(["--target", "tesseract"]) == 2


def test_target_all_skips_the_installer_and_still_stops_on_a_bad_pin(fetch, monkeypatch):
    cfg = {"tesseract": dict(_real()["tesseract"]), "poppler": _meta(PLACEHOLDER)}
    monkeypatch.setattr(rb, "_load_binaries", lambda: cfg)
    assert rb.main(["--target", "all"]) == 2     # poppler's placeholder, not the skip
    assert fetch == []


# ---------------------------------------------------------------------------
# build.py: early toml check, licence step
# ---------------------------------------------------------------------------

def _toml(tmp_path, body):
    p = tmp_path / "binaries.toml"
    p.write_text(body, encoding="utf-8")
    return p


def test_build_check_fails_on_a_placeholder(tmp_path):
    p = _toml(tmp_path, f'[x]\nsha256 = "{PLACEHOLDER}"\n')
    problems = bld.check_binaries_toml(p)
    assert problems and "placeholder" in problems[0]


def test_build_check_fails_on_a_placeholder_in_a_nested_table_and_source_sha(tmp_path):
    p = _toml(tmp_path, f'[t.eng]\nsha256 = "{GOOD_SHA}"\n[p]\nsha256 = "{GOOD_SHA}"\n'
                        f'source_sha256 = "{PLACEHOLDER}"\n')
    assert any("source_sha256" in x for x in bld.check_binaries_toml(p))
    p = _toml(tmp_path, f'[t.eng]\nsha256 = "{PLACEHOLDER}"\n')
    assert any("[t.eng]" in x for x in bld.check_binaries_toml(p))


def test_build_check_fails_on_a_malformed_hash(tmp_path):
    assert bld.check_binaries_toml(_toml(tmp_path, '[x]\nsha256 = "abc"\n'))
    assert bld.check_binaries_toml(_toml(tmp_path, f'[x]\nsha256 = "{"A" * 64}"\n'))


def test_build_check_passes_a_good_toml(tmp_path):
    assert bld.check_binaries_toml(_toml(tmp_path, f'[x]\nsha256 = "{GOOD_SHA}"\n')) == []


def test_step0_exits_nonzero_on_a_placeholder(tmp_path, monkeypatch):
    p = _toml(tmp_path, f'[x]\nsha256 = "{PLACEHOLDER}"\n')
    monkeypatch.setattr(bld, "BINARIES_TOML", p)
    with pytest.raises(SystemExit) as e:
        bld.step0_check_binaries_toml(bld._Log(False))
    assert e.value.code == 2


def test_main_runs_the_toml_check_before_anything_else():
    src = inspect.getsource(bld.main)
    assert src.index("step0_check_binaries_toml(") < src.index("step1_resolve_version(")


# ---------------------------------------------------------------------------
# Upstream-version comparison (fixed inputs) and the workflow
# ---------------------------------------------------------------------------

PINS = {"tesseract": "5.5.0.20241111", "poppler": "24.07.0", "qpdf": "11.9.1"}


def test_no_report_when_every_pin_is_current():
    lines, notes = nbu.updates(PINS, {"tesseract": "5.5.0", "poppler": "v24.07.0-0", "qpdf": "v11.9.1"})
    assert lines == [] and notes == []


def test_report_when_upstream_moves():
    lines, _ = nbu.updates(PINS, {"tesseract": "5.5.4", "poppler": "v24.07.0-0", "qpdf": "v11.9.1"})
    assert len(lines) == 1 and "Tesseract" in lines[0] and "`5.5.4`" in lines[0]
    assert "tesseract-ocr/tesseract/releases" in lines[0]
    lines, _ = nbu.updates(PINS, {"tesseract": "5.5.0", "poppler": "v25.01.0-0", "qpdf": "v12.0.0"})
    assert [("Poppler" in x) or ("qpdf" in x) for x in lines] == [True, True]


def test_an_older_or_missing_or_garbage_tag_is_never_reported():
    for tag in ("5.4.0", "5.3.9", "", "nightly"):
        lines, _ = nbu.updates(PINS, {"tesseract": tag, "poppler": "", "qpdf": ""})
        assert lines == [], tag


def test_unparseable_tag_leaves_a_note():
    _, notes = nbu.updates(PINS, {"tesseract": "nightly", "poppler": "", "qpdf": ""})
    assert any("tesseract" in n for n in notes)


def test_the_real_pins_produce_no_report_against_their_own_release():
    pinned = nbu.pinned_versions(REAL_TOML.read_text(encoding="utf-8"))
    lines, notes = nbu.updates(pinned, {"tesseract": "5.5.0", "poppler": "v24.07.0-0", "qpdf": "v11.9.1"})
    assert lines == [] and notes == []


def test_script_main_prints_nothing_for_current_pins_and_a_line_for_a_newer_tag(capsys):
    args = ["--toml", str(REAL_TOML), "--poppler", "v24.07.0-0", "--qpdf", "v11.9.1"]
    assert nbu.main(args + ["--tesseract", "5.5.0"]) == 0
    assert capsys.readouterr().out.strip() == ""
    assert nbu.main(args + ["--tesseract", "5.5.4"]) == 0
    assert "5.5.4" in capsys.readouterr().out


def test_workflow_checks_the_upstream_repo_and_tells_the_truth_about_updating():
    wf = WORKFLOW.read_text(encoding="utf-8")
    assert "repos/tesseract-ocr/tesseract/releases/latest" in wf
    assert "UB-Mannheim" not in wf
    assert "native_binary_updates.py" in wf
    assert "--from-tesseract" in wf                      # the Tesseract route
    assert "Paste the new SHA256" not in wf              # the old, untrue step
