"""
tests/test_review_engine.py — regression guards for ui/_review_engine.py.

Background: ui/_review_engine.py factors out the shared "needs review" table
engine that used to be copy-pasted between ui/tabs/gnucash_review.py and
ui/tabs/itr_mapping_review.py (identical _js_json, identical %%TOKEN%% HTML
template, identical eval-bootstrap, identical payload bridge). These tests
cover the public API surface: safe JSON embedding, row-metadata
normalisation, %%TOKEN%% substitution completeness, HTML escaping of
untrusted row content, app_id namespacing, Column sort-order lowercasing,
and payload parsing.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ui"))

from ui import _review_engine as engine  # noqa: E402


# ---------------------------------------------------------------------------
# js_json()
# ---------------------------------------------------------------------------

def test_js_json_escapes_script_close_tag():
    out = engine.js_json({"x": "</script><script>alert(1)</script>"})
    assert "</script>" not in out
    assert "\\u003c" in out and "\\u003e" in out


def test_js_json_escapes_angle_brackets_and_ampersand():
    out = engine.js_json("<img src=x onerror=alert(1)> & more")
    assert "<" not in out
    assert ">" not in out
    assert "&" not in out
    assert "\\u003c" in out
    assert "\\u003e" in out
    assert "\\u0026" in out


def test_js_json_still_valid_json_after_escaping():
    import json
    value = {"a": "<b>&</b>"}
    out = engine.js_json(value)
    # Reverse the escapes the way a JS engine effectively sees the string
    # (json.loads only needs the JSON structure to be intact; < etc are
    # valid unicode escapes and decode transparently).
    assert json.loads(out) == value


# ---------------------------------------------------------------------------
# prepare_rows()
# ---------------------------------------------------------------------------

def test_prepare_rows_fills_every_meta_key():
    rows = [{"a": 1}]
    out = engine.prepare_rows(rows)
    assert len(out) == 1
    for key in (
        engine.META_TAGS,
        engine.META_ROWCLASS,
        engine.META_BADGES,
        engine.META_LOCKED,
        engine.META_NOTE,
    ):
        assert key in out[0]
    assert out[0][engine.META_TAGS] == []
    assert out[0][engine.META_ROWCLASS] == ""
    assert out[0][engine.META_BADGES] == {}
    assert out[0][engine.META_LOCKED] is False
    assert out[0][engine.META_NOTE] == ""


def test_prepare_rows_preserves_existing_meta_values():
    rows = [{"a": 1, "_tags": ["x"], "_locked": True}]
    out = engine.prepare_rows(rows)
    assert out[0]["_tags"] == ["x"]
    assert out[0]["_locked"] is True


def test_prepare_rows_does_not_mutate_caller_dicts():
    row = {"a": 1}
    rows = [row]
    engine.prepare_rows(rows)
    assert row == {"a": 1}
    assert "_tags" not in row


# ---------------------------------------------------------------------------
# payload_box_css()
# ---------------------------------------------------------------------------

def test_payload_box_css_emits_given_elem_id():
    css = engine.payload_box_css("myapp-payload-box")
    assert "#myapp-payload-box" in css
    assert "<style>" in css


# ---------------------------------------------------------------------------
# Column
# ---------------------------------------------------------------------------

def test_column_sort_order_lowercased_in_json():
    col = engine.Column(key="conf", label="Confidence", sort="order", order=("HIGH", "Medium", "low"))
    d = engine._col_dict(col)
    assert d["order"] == ["high", "medium", "low"]


# ---------------------------------------------------------------------------
# build_html()
# ---------------------------------------------------------------------------

def _make_spec(app_id="app1"):
    return engine.ReviewSpec(
        app_id=app_id,
        columns=[
            engine.Column(key="Security", label="Security"),
            engine.Column(key="Account", label="Account"),
        ],
        target_col="Account",
        payload_var=f"_{app_id}Payload",
        picker_items=[engine.PickerItem(value="Assets:X", primary="Assets:X")],
        status_options=[("suspense", "Suspense")],
        apply_matching_on="Security",
    )


def test_build_html_leaves_no_unreplaced_tokens():
    spec = _make_spec()
    rows = [{"Security": "INFY", "Account": "Assets:Investments"}]
    html = engine.build_html(spec, rows)
    leftover = re.findall(r"%%[A-Z_]+%%", html)
    assert leftover == []


def test_build_html_escapes_untrusted_row_content():
    spec = _make_spec()
    rows = [{"Security": "<img src=x onerror=alert(1)>", "Account": "Assets:X"}]
    html = engine.build_html(spec, rows)
    # The raw payload is embedded as JSON for the client-side renderer to
    # escape at innerHTML time, so it must not appear as literal live markup
    # (i.e. not immediately followed by a real '>' outside of a JSON string
    # escape). Simplest strong guard: an *unescaped* live <img ...> tag with
    # onerror must not appear in the document outside the JSON data blob's
    # escaped form.
    assert "<img src=x onerror=alert(1)>" not in html


def test_build_html_json_data_carries_value_but_document_has_no_live_script_break():
    # The row content is legitimately present (as JSON string data for the
    # client to escape at render time), but it must never let an attacker
    # close out of the surrounding <script> tag.
    spec = _make_spec()
    rows = [{"Security": "</script><script>alert(1)</script>", "Account": "Assets:X"}]
    html = engine.build_html(spec, rows)
    assert "</script><script>alert(1)</script>" not in html


def test_build_html_namespaces_ids_by_app_id_no_collisions():
    spec_a = _make_spec("appA")
    spec_b = _make_spec("appB")
    rows = [{"Security": "INFY", "Account": "Assets:Investments"}]
    html_a = engine.build_html(spec_a, rows)
    html_b = engine.build_html(spec_b, rows)

    ids_a = set(re.findall(r'id="([^"]+)"', html_a))
    ids_b = set(re.findall(r'id="([^"]+)"', html_b))

    assert ids_a, "expected some ids to be emitted"
    assert ids_b, "expected some ids to be emitted"
    assert ids_a.isdisjoint(ids_b)
    assert all(i.startswith("appA") for i in ids_a)
    assert all(i.startswith("appB") for i in ids_b)


def test_build_html_includes_payload_box_id_property():
    spec = _make_spec("appC")
    assert spec.payload_box_id == "appC-payload-box"


def _extract_const(html: str, name: str):
    """Pull a `const NAME = <json>;` value back out of build_html() output."""
    import json
    m = re.search(r"const " + re.escape(name) + r"\s*=\s*(.*?);\n", html)
    assert m, f"const {name} not found in html"
    return json.loads(m.group(1))


def test_build_html_also_set_matching_falls_back_to_also_set_when_unset():
    """Backward-compatibility guarantee (TASK A): a spec declaring only
    `also_set` (as three of the four screens do) must apply those same
    values for BOTH the ordinary apply and the "apply to matching" action."""
    spec = _make_spec()
    spec.also_set = {"Confidence": "override"}
    html = engine.build_html(spec, [{"Security": "INFY", "Account": "Assets:X"}])
    also_set = _extract_const(html, "ALSO_SET")
    also_set_matching = _extract_const(html, "ALSO_SET_MATCHING")
    assert also_set == {"Confidence": "override"}
    assert also_set_matching == also_set


def test_build_html_also_set_matching_distinct_when_declared():
    """TASK A: a spec that declares also_set_matching gets a DIFFERENT
    provenance blob embedded for the "apply to matching" action."""
    spec = _make_spec()
    spec.also_set = {"Confidence": "override", "MatchReason": "User override (review)"}
    spec.also_set_matching = {"Confidence": "override", "MatchReason": "User override (batch match)"}
    html = engine.build_html(spec, [{"Security": "INFY", "Account": "Assets:X"}])
    also_set = _extract_const(html, "ALSO_SET")
    also_set_matching = _extract_const(html, "ALSO_SET_MATCHING")
    assert also_set["MatchReason"] == "User override (review)"
    assert also_set_matching["MatchReason"] == "User override (batch match)"
    assert also_set != also_set_matching


def test_build_html_works_with_no_status_options_or_apply_matching():
    spec = engine.ReviewSpec(
        app_id="bare",
        columns=[engine.Column(key="a", label="A")],
        target_col="a",
        payload_var="_barePayload",
    )
    html = engine.build_html(spec, [{"a": "x"}])
    leftover = re.findall(r"%%[A-Z_]+%%", html)
    assert leftover == []
    # No apply-match *button* is emitted when apply_matching_on is unset,
    # even though the JS still safely no-ops via $('apply-match') === null.
    assert 'id="bare-apply-match"' not in html


# ---------------------------------------------------------------------------
# parse_payload()
# ---------------------------------------------------------------------------

def test_parse_payload_empty_string_returns_all_keys_empty():
    out = engine.parse_payload("")
    assert out == {"context": {}, "changes": [], "all_rows": []}


def test_parse_payload_blank_whitespace_returns_all_keys_empty():
    out = engine.parse_payload("   \n  ")
    assert out == {"context": {}, "changes": [], "all_rows": []}


def test_parse_payload_roundtrip():
    import json
    raw = json.dumps({"context": {"k": "v"}, "changes": [{"a": 1}], "all_rows": [{"a": 1}]})
    out = engine.parse_payload(raw)
    assert out["context"] == {"k": "v"}
    assert out["changes"] == [{"a": 1}]
    assert out["all_rows"] == [{"a": 1}]


def test_parse_payload_missing_keys_default_to_empty():
    out = engine.parse_payload("{}")
    assert out == {"context": {}, "changes": [], "all_rows": []}


def test_parse_payload_malformed_json_raises_value_error():
    import pytest
    with pytest.raises(ValueError):
        engine.parse_payload("{not valid json")


def test_parse_payload_non_object_top_level_raises_value_error():
    import pytest
    with pytest.raises(ValueError):
        engine.parse_payload("[1, 2, 3]")
    with pytest.raises(ValueError):
        engine.parse_payload('"just a string"')
