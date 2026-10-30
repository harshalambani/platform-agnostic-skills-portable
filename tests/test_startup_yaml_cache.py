"""Start-up speed work: C YAML loader + in-process cache + deferred tab imports.

Each guard has a negative twin: the cache must never serve a stale file or a
shared mutable object, the loader fallback must be silent, and building the
UI must not import the modules whose import was deferred to first use.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

from agents import yaml_cache

_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _fresh_cache():
    yaml_cache.clear_cache()
    yield
    yaml_cache.clear_cache()


def _tracked_yaml_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "*.yaml", "*.yml"], cwd=_ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    return [_ROOT / p for p in out if not p.replace("\\", "/").startswith("Data/")]


# --- C loader and pure loader read every tracked YAML identically ----------

@pytest.mark.skipif(not hasattr(yaml, "CSafeLoader"), reason="PyYAML built without libyaml")
def test_c_and_pure_loaders_agree_on_every_tracked_yaml():
    files = _tracked_yaml_files()
    assert files, "expected tracked YAML files"
    for f in files:
        text = f.read_text(encoding="utf-8")
        assert yaml.load(text, Loader=yaml.CSafeLoader) == yaml.load(text, Loader=yaml.SafeLoader), f


def test_helper_matches_safe_load_on_every_tracked_yaml():
    for f in _tracked_yaml_files():
        assert yaml_cache.load_yaml_file(f) == yaml.safe_load(f.read_text(encoding="utf-8")), f


def test_helper_uses_the_c_loader_when_available():
    if hasattr(yaml, "CSafeLoader"):
        assert yaml_cache._LOADER is yaml.CSafeLoader
    else:
        assert yaml_cache._LOADER is yaml.SafeLoader


def test_fallback_to_safe_loader_when_c_loader_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(yaml_cache, "_LOADER", yaml.SafeLoader)
    f = tmp_path / "a.yaml"
    f.write_text("a: 1\nb: [x, y]\n", encoding="utf-8")
    assert yaml_cache.load_yaml_file(f) == {"a": 1, "b": ["x", "y"]}


def test_module_falls_back_when_yaml_has_no_csafeloader():
    code = textwrap.dedent(f"""
        import sys, yaml
        sys.path.insert(0, r"{(_ROOT / 'src').as_posix()}")
        if hasattr(yaml, "CSafeLoader"):
            del yaml.CSafeLoader
        from agents import yaml_cache
        assert yaml_cache._LOADER is yaml.SafeLoader
        import pathlib, tempfile
        p = pathlib.Path(tempfile.mkdtemp()) / "x.yaml"
        p.write_text("k: v\\n", encoding="utf-8")
        assert yaml_cache.load_yaml_file(p) == {{"k": "v"}}
    """)
    r = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# --- cache correctness (negative tests) -------------------------------------

def _bump_mtime(f: Path) -> None:
    st = f.stat()
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000))


def test_edited_file_is_not_served_stale(tmp_path):
    f = tmp_path / "c.yaml"
    f.write_text("v: 1\n", encoding="utf-8")
    assert yaml_cache.load_yaml_file(f) == {"v": 1}
    # Same size, new content; force a different mtime so the test does not
    # depend on the file system's timestamp granularity.
    f.write_text("v: 2\n", encoding="utf-8")
    _bump_mtime(f)
    assert yaml_cache.load_yaml_file(f) == {"v": 2}
    f.write_text("v: 3\nextra: yes\n", encoding="utf-8")  # different size too
    assert yaml_cache.load_yaml_file(f) == {"v": 3, "extra": True}


def test_cache_is_hit_when_file_unchanged(tmp_path, monkeypatch):
    f = tmp_path / "h.yaml"
    f.write_text("v: 1\n", encoding="utf-8")
    yaml_cache.load_yaml_file(f)
    calls = []
    monkeypatch.setattr(yaml_cache, "_parse", lambda t: calls.append(t))
    assert yaml_cache.load_yaml_file(f) == {"v": 1}
    assert calls == []


def test_mutating_a_result_does_not_leak_into_the_next_load(tmp_path):
    f = tmp_path / "m.yaml"
    f.write_text("a:\n  - 1\n  - 2\nb: {c: d}\n", encoding="utf-8")
    first = yaml_cache.load_yaml_file(f)
    first["a"].append(99)
    first["b"]["c"] = "changed"
    first["new"] = True
    second = yaml_cache.load_yaml_file(f)
    assert second == {"a": [1, 2], "b": {"c": "d"}}
    assert second is not first


def test_parse_errors_are_raised_and_not_cached(tmp_path):
    f = tmp_path / "bad.yaml"
    f.write_text("a: [unclosed\n", encoding="utf-8")
    with pytest.raises(yaml.YAMLError):
        yaml_cache.load_yaml_file(f)
    f.write_text("a: [closed]\n", encoding="utf-8")
    assert yaml_cache.load_yaml_file(f) == {"a": ["closed"]}


def test_missing_file_raises_oserror(tmp_path):
    with pytest.raises(OSError):
        yaml_cache.load_yaml_file(tmp_path / "nope.yaml")


def test_stale_generations_do_not_pile_up(tmp_path):
    f = tmp_path / "g.yaml"
    for i in range(5):
        f.write_text(f"v: {i}\n" + "#" * i, encoding="utf-8")
        yaml_cache.load_yaml_file(f)
    assert len(yaml_cache._cache) == 1


def test_portable_config_is_reread_after_a_save(tmp_path):
    from ui import _config

    cfg = tmp_path / "settings" / "config.yaml"
    _config.write_portable_config({"k": "one"}, cfg)
    assert _config.load_portable_config(cfg) == {"k": "one"}
    _config.write_portable_config({"k": "two", "n": 5}, cfg)
    assert _config.load_portable_config(cfg) == {"k": "two", "n": 5}


def test_skill_manifest_edit_is_picked_up_by_a_refresh(tmp_path, monkeypatch):
    from agents import registry

    root = tmp_path / "agents"
    d = root / "skill_demo"
    d.mkdir(parents=True)
    manifest = d / "skill.yaml"
    body = ("name: demo\ndisplay_name: {dn}\ndescription: d\nmode: single\n"
            "entry_point: x:y\n")
    manifest.write_text(body.format(dn="Alpha"), encoding="utf-8")
    monkeypatch.setattr(registry, "_AGENTS_ROOT", root)
    monkeypatch.setattr(registry, "_cache", None)
    assert [s.display_name for s in registry.discover(refresh=True)] == ["Alpha"]
    manifest.write_text(body.format(dn="Bravo"), encoding="utf-8")
    _bump_mtime(manifest)
    assert [s.display_name for s in registry.discover(refresh=True)] == ["Bravo"]
    manifest.unlink()
    assert registry.discover(refresh=True) == []


# --- deferred imports --------------------------------------------------------

_BUILD_PROBE = textwrap.dedent("""
    import sys, json
    sys.path.insert(0, r"{root}"); sys.path.insert(0, r"{root}/src")
    from ui import webui
    webui.build_app(launch=False)
    deferred = ["schedules", "tax_core", "interest_234", "advance_tax",
                "agents.skill_gnucash_pipeline.agent"]
    print(json.dumps({{m: (m in sys.modules) for m in deferred}}))
""")


def test_building_the_ui_does_not_import_the_deferred_modules(tmp_path):
    pytest.importorskip("gradio")
    env = {k: v for k, v in os.environ.items() if k != "PA_SKILLS_CONFIG"}
    r = subprocess.run(
        [sys.executable, "-I", "-c", _BUILD_PROBE.format(root=_ROOT.as_posix())],
        capture_output=True, text=True, cwd=tmp_path, env=env, timeout=240,
    )
    assert r.returncode == 0, r.stderr[-2000:]
    loaded = json.loads(r.stdout.strip().splitlines()[-1])
    assert not any(loaded.values()), f"imported during UI build: {[m for m, v in loaded.items() if v]}"


def test_advance_tax_estimate_list_fills_on_first_use(tmp_path, monkeypatch):
    """The dropdown starts with only '(new estimate)'; the first-use handlers
    (tab select / Refresh) import the estimator and list the saved estimates."""
    pytest.importorskip("gradio")
    from ui.tabs import advance_tax

    monkeypatch.setattr(advance_tax._config_mod, "data_root_dir", lambda: tmp_path)
    adv = advance_tax._adv_mod()
    est_dir = adv._estimate_dir(str(tmp_path))
    est_dir.mkdir(parents=True, exist_ok=True)
    (est_dir / "alpha.json").write_text("{}", encoding="utf-8")
    choices = advance_tax._estimate_choices()
    assert choices[0] == ("(new estimate)", "")
    assert ("alpha", "alpha") in choices
