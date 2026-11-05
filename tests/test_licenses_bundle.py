"""
licenses-bundle.txt: the generator (synthetic metadata) and the build step that
calls it. A package with no licence text is listed under its own heading, and a
generator failure fails the build.
"""
from __future__ import annotations

import importlib.util
import inspect
import sys
from pathlib import Path, PurePosixPath

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


bld = _load("pask_build_lic", ROOT / "bundling" / "build.py")
lb = _load("pask_licenses_bundle_t", ROOT / "bundling" / "licenses_bundle.py")


def test_main_runs_the_licence_step_after_pyinstaller_and_before_the_binaries():
    src = inspect.getsource(bld.main)
    assert src.index("step4_run_pyinstaller(") < src.index("step4b_licenses_bundle(") \
        < src.index("step5_native_binaries(")


def test_build_calls_the_generator_with_the_build_venv_python(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(bld, "STAGING", tmp_path / "staging")

    def fake_run(cmd, **kw):
        seen["cmd"] = [str(c) for c in cmd]
        out = Path(cmd[cmd.index("--out") + 1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("bundle", encoding="utf-8")

    monkeypatch.setattr(bld, "_run", fake_run)
    py = tmp_path / "venv" / "python.exe"
    out = bld.step4b_licenses_bundle(py, bld._Log(False))
    assert seen["cmd"][0] == str(py)                         # the venv's python
    assert str(bld.LICENSES_BUNDLE_PY) in seen["cmd"]
    assert str(bld.REQUIREMENTS_LOCK) in seen["cmd"]
    assert out == tmp_path / "staging" / "App" / "PASkills" / "licenses-bundle.txt"


def test_a_generator_that_writes_nothing_fails_the_build(tmp_path, monkeypatch):
    monkeypatch.setattr(bld, "STAGING", tmp_path / "staging")
    monkeypatch.setattr(bld, "_run", lambda cmd, **kw: None)     # "succeeds", no file
    with pytest.raises(SystemExit) as e:
        bld.step4b_licenses_bundle(tmp_path / "py", bld._Log(False))
    assert e.value.code == 2


def test_a_failing_generator_fails_the_build(tmp_path, monkeypatch):
    """The real _run exits 2 on a non-zero command; prove that, end to end."""
    monkeypatch.setattr(bld, "STAGING", tmp_path / "staging")
    bad = tmp_path / "bad_gen.py"
    bad.write_text("import sys; sys.exit(3)\n", encoding="utf-8")
    monkeypatch.setattr(bld, "LICENSES_BUNDLE_PY", bad)
    with pytest.raises(SystemExit) as e:
        bld.step4b_licenses_bundle(Path(sys.executable), bld._Log(False))
    assert e.value.code == 2


# ---------------------------------------------------------------------------
# licenses_bundle.py (synthetic metadata)
# ---------------------------------------------------------------------------

class _Meta:
    def __init__(self, fields, classifiers=()):
        self._f, self._c = fields, list(classifiers)

    def get(self, k, default=None):
        return self._f.get(k, default)

    def __getitem__(self, k):
        return self._f[k]

    def get_all(self, k):
        return self._c if k == "Classifier" else None


class _Dist:
    def __init__(self, name, version, texts=None, licence=None, expr=None, classifiers=()):
        fields = {"Name": name}
        if licence is not None:
            fields["License"] = licence
        if expr is not None:
            fields["License-Expression"] = expr
        self.metadata = _Meta(fields, classifiers)
        self.version = version
        self._texts = texts or {}
        self.files = [PurePosixPath(f"{name}-{version}.dist-info/{n}") for n in self._texts] \
            + [PurePosixPath(f"{name}/__init__.py")]

    def locate_file(self, f):
        class _P:
            def __init__(s, text):
                s.text = text

            def read_text(s, **kw):
                return s.text
        return _P(self._texts[f.name]) if f.name in self._texts else _P("code")


LOCK = ("alpha==1.0 \\\n    --hash=sha256:" + "0" * 64 + "\nbeta-pkg==2.1\ngamma==3.0 ; sys_platform == 'win32'\n"
        "# comment==9\n")


def test_locked_names_reads_hashes_markers_and_ignores_comments():
    assert lb.locked_names(LOCK) == ["alpha", "beta-pkg", "gamma"]


def test_bundle_has_name_version_licence_field_and_text():
    dists = [_Dist("alpha", "1.0", {"LICENSE": "ALPHA LICENCE TEXT"}, licence="MIT"),
             _Dist("beta_pkg", "2.1", {"COPYING.txt": "BETA TEXT"}, expr="Apache-2.0"),
             _Dist("gamma", "3.0", {"LICENSE": "G"}, classifiers=["License :: OSI Approved :: BSD License"])]
    text, records = lb.build_bundle(LOCK, dists)
    assert [r.name for r in records] == ["alpha", "beta_pkg", "gamma"]
    for needle in ("alpha 1.0", "Declared licence: MIT", "ALPHA LICENCE TEXT",
                   "beta_pkg 2.1", "Apache-2.0", "BETA TEXT", "BSD License"):
        assert needle in text


def test_package_with_no_licence_text_gets_its_own_heading_and_is_not_skipped():
    dists = [_Dist("alpha", "1.0", {"LICENSE": "ALPHA TEXT"}, licence="MIT"),
             _Dist("beta_pkg", "2.1", licence="BSD-3-Clause"),      # no licence file
             _Dist("gamma", "3.0")]                                  # nothing declared at all
    text, records = lb.build_bundle(LOCK, dists)
    assert len(records) == 3
    head = text.index("2. Packages with NO licence text in their metadata", text.index("Contents") + 200)
    tail = text[head:]
    assert "beta_pkg 2.1 - declared licence: BSD-3-Clause" in tail
    assert "gamma 3.0 - declared licence: (none declared)" in tail
    assert "alpha" not in tail                                   # one with text is not in the list
    assert "without licence text: 2" in text


def test_a_locked_package_that_is_not_installed_is_an_error_not_a_skip():
    with pytest.raises(LookupError, match="gamma"):
        lb.build_bundle(LOCK, [_Dist("alpha", "1.0"), _Dist("beta_pkg", "2.1")])


def test_empty_lock_is_an_error():
    with pytest.raises(LookupError):
        lb.build_bundle("# nothing\n", [])


def test_main_returns_2_and_writes_nothing_when_a_package_is_missing(tmp_path, monkeypatch, capsys):
    lock = tmp_path / "lock.txt"
    lock.write_text("definitely-not-installed-pkg-xyz==1.0\n", encoding="utf-8")
    out = tmp_path / "out" / "licenses-bundle.txt"
    assert lb.main(["--lock", str(lock), "--out", str(out)]) == 2
    assert not out.exists()
    assert "FAILED" in capsys.readouterr().err


def test_main_writes_the_file_for_an_installed_package(tmp_path):
    lock = tmp_path / "lock.txt"
    lock.write_text("pytest==0\n", encoding="utf-8")
    out = tmp_path / "licenses-bundle.txt"
    assert lb.main(["--lock", str(lock), "--out", str(out)]) == 0
    assert "pytest" in out.read_text(encoding="utf-8")


def test_long_pasted_licence_field_is_cut():
    d = _Dist("alpha", "1.0", licence="x" * 5000)
    assert len(lb.declared_licence(d)) < 300
