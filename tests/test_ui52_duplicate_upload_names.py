"""UI-52: two uploads with the same file name must never stage as one."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import test_input_caps as caps  # noqa: E402  (reuse its handler-driving helpers)

import ui.tabs._generic as generic  # noqa: E402


def _upload(tmp_path, folder, name, content):
    d = tmp_path / folder
    d.mkdir(exist_ok=True)
    f = d / name
    f.write_bytes(content)
    return caps._fake_file(f)


@pytest.fixture
def spy(monkeypatch):
    seen = {"staged": None, "calls": 0}

    def fake_load(skill):
        def run_fn(**kw):
            seen["calls"] += 1
            return "ok"
        return run_fn

    def fake_kwargs(skill, input_map, out_path, legacy_cfg, model_choice, work_dir):
        d = Path(input_map["input_files"])
        seen["staged"] = {p.name: p.read_bytes() for p in d.iterdir()}
        return {}

    import agents.registry as registry
    monkeypatch.setattr(registry, "load_run_function", fake_load)
    monkeypatch.setattr(generic, "build_run_kwargs", fake_kwargs)
    return seen


def _skill():
    s = caps._make_minimal_skill("files")
    s.requires.llm = False
    return s


def _texts(results):
    return [md for md, *_ in results]


def test_different_names_stage_both_and_the_skill_gets_both(tmp_path, spy):
    ups = [_upload(tmp_path, "a", "one.xls", b"1"), _upload(tmp_path, "b", "two.xls", b"2")]
    results = caps._run_handler_to_list(_skill(), ups, "m")
    assert spy["staged"] == {"one.xls": b"1", "two.xls": b"2"}
    assert spy["calls"] == 1
    assert not any("both named" in t for t in _texts(results))


def test_same_name_twice_stops_and_the_skill_never_runs(tmp_path, spy):
    ups = [_upload(tmp_path, "a", "s.xls", b"first"), _upload(tmp_path, "b", "s.xls", b"second")]
    results = caps._run_handler_to_list(_skill(), ups, "m")
    last = _texts(results)[-1]
    assert "two uploads are both named **s.xls**" in last
    assert "Nothing was run" in last
    assert spy["calls"] == 0 and spy["staged"] is None


def test_names_differing_only_in_case_stop(tmp_path, spy):
    ups = [_upload(tmp_path, "a", "A.xls", b"1"), _upload(tmp_path, "b", "a.xls", b"2")]
    results = caps._run_handler_to_list(_skill(), ups, "m")
    assert "both named" in _texts(results)[-1]
    assert spy["calls"] == 0 and spy["staged"] is None


def test_one_upload_is_unchanged(tmp_path, spy):
    results = caps._run_handler_to_list(_skill(), [_upload(tmp_path, "a", "s.xls", b"1")], "m")
    assert spy["staged"] == {"s.xls": b"1"}
    assert spy["calls"] == 1
    assert not any("both named" in t for t in _texts(results))
