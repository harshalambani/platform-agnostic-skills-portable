"""CC-07: the CC Sort result is a short summary, not a list of file names."""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "src"), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from agents.skill_cc_sort import agent as cc_agent  # noqa: E402
from agents.skill_cc_sort import completeness as C  # noqa: E402

PERIOD = (date(2025, 4, 1), date(2026, 3, 31), "FY 2025-26")


def _months():
    out = []
    y, m = 2025, 4
    for _ in range(12):
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        out.append((date(y, m, 1), date(ny, nm, 1).fromordinal(date(ny, nm, 1).toordinal() - 1)))
        y, m = ny, nm
    return out


FULL = {f"s{i}.pdf": f"{s:%d/%m/%Y} To {e:%d/%m/%Y}" for i, (s, e) in enumerate(_months())}


def _tree(tmp_path, files):
    root = tmp_path / "out"
    for rel in files:
        p = root / "Decrypted_PDFs_Correct" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
    return root


def _run(tmp_path, monkeypatch, files, texts, failed=()):
    root = _tree(tmp_path, files)
    (root / "sort_results.json").write_text(json.dumps({"failed_decryption": list(failed)}))
    monkeypatch.setattr(cc_agent.shutil, "which", lambda _n: "qpdf")
    monkeypatch.setattr(cc_agent, "_run_script", lambda *_a, **_k: 0)
    monkeypatch.setattr(C, "read_page1_text", lambda p: texts.get(p.name))
    msg = cc_agent.run(str(tmp_path / "in"), str(root), "pw", "FY 2025-26")
    return msg, root


def test_leaflets_and_duplicates_are_counts_only_and_not_issues(tmp_path, monkeypatch):
    files = [f"Synth-Gold/{n}" for n in FULL]
    files += [f"Synth-Gold/terms_leaflet_{i}.pdf" for i in range(5)]
    msg, root = _run(tmp_path, monkeypatch, files, FULL)
    assert msg.startswith("Sort completed successfully.")
    assert "5 file(s)" in msg
    assert "terms_leaflet_0" not in msg
    # the full names go to the saved report
    saved = (root / "completeness_report.txt").read_text()
    assert "terms_leaflet_0.pdf" in saved and "terms_leaflet_4.pdf" in saved


def test_duplicates_never_raise_the_issue_count(tmp_path, monkeypatch):
    files = [f"Synth-Gold/{n}" for n in FULL] + ["Synth-Gold/copy_of_s0.pdf", "Synth-Gold/copy_of_s1.pdf"]
    texts = dict(FULL, **{"copy_of_s0.pdf": FULL["s0.pdf"], "copy_of_s1.pdf": FULL["s1.pdf"]})
    msg, root = _run(tmp_path, monkeypatch, files, texts)
    assert msg.startswith("Sort completed successfully.")
    assert "Duplicates (same period; counted once): 2 file(s)" in msg
    assert "copy_of_s0" not in msg and "copy_of_s0.pdf" in (root / "completeness_report.txt").read_text()


def test_missing_month_always_shown_among_many_leaflets_and_duplicates(tmp_path, monkeypatch):
    files = [f"Synth-Gold/{n}" for n in FULL if n != "s4.pdf"]
    files += [f"Synth-Gold/leaflet_{i}.pdf" for i in range(30)]
    files += ["Synth-Gold/dup.pdf"]
    texts = {k: v for k, v in FULL.items() if k != "s4.pdf"}
    texts["dup.pdf"] = FULL["s0.pdf"]
    msg, _root = _run(tmp_path, monkeypatch, files, texts)
    assert "completed with 1 issue" in msg
    assert "Aug 2025" in msg
    assert "statement(s) found" in msg


def test_not_a_statement_folder_is_never_an_issue_nor_unknown(tmp_path, monkeypatch):
    files = [f"Synth-Gold/{n}" for n in FULL] + [f"{C.NOT_STATEMENT_FOLDER}/year_end.pdf"]
    msg, root = _run(tmp_path, monkeypatch, files, FULL)
    assert msg.startswith("Sort completed successfully.")
    assert "Unknown-Unknown" not in msg
    assert "1 file(s)" in msg


def test_unknown_folder_still_an_issue_but_by_count(tmp_path, monkeypatch):
    files = [f"Synth-Gold/{n}" for n in FULL] + ["Unknown-Unknown/mystery_4315.pdf"]
    msg, root = _run(tmp_path, monkeypatch, files, FULL)
    assert "completed with 1 issue" in msg
    assert "mystery_4315" not in msg                       # no file names / card fragments
    assert "mystery_4315.pdf" in (root / "completeness_report.txt").read_text()


def test_panel_never_prints_decrypt_failure_names(tmp_path, monkeypatch):
    msg, _ = _run(tmp_path, monkeypatch, [f"Synth-Gold/{n}" for n in FULL], FULL,
                  failed=["card_5678_locked.pdf"])
    assert "could not decrypt 1 file(s)" in msg
    assert "5678" not in msg


# ---- the shared rule ---------------------------------------------------------

def _stub_pdf(monkeypatch, lines):
    import importlib.util
    script = (ROOT / "src" / "agents" / "skill_cc_transactions" / "scripts"
              / "create_cc_transaction_list.py")
    return script, lines


def test_year_end_and_leaflet_recognised_via_the_shared_rule(tmp_path, monkeypatch):
    """non_statement_title reuses create_cc_transaction_list.non_statement_reason."""
    import importlib.util
    script = (ROOT / "src" / "agents" / "skill_cc_transactions" / "scripts"
              / "create_cc_transaction_list.py")
    real_spec = importlib.util.spec_from_file_location

    docs = {
        "year_end.pdf": ["Year End Statement & Summary", "Account Summary for the period from 01 Apr 2025 to 31 Mar 2026"],
        "leaflet.pdf": ["Most Important Terms and Conditions", "Fee table"],
        "real.pdf": ["Statement Date:10/05/2025", "10/05/2025 SHOP 100.00"],
    }

    def fake_read_lines(path):
        return docs[Path(path).name]

    # Load the module once the same way non_statement_title does, patch read_lines on
    # every fresh load by wrapping the loader.
    def spec_patch(name, location, *a, **k):
        spec = real_spec(name, location, *a, **k)
        orig = spec.loader.exec_module

        def exec_module(mod):
            orig(mod)
            mod.read_lines = fake_read_lines
        spec.loader.exec_module = exec_module
        return spec

    monkeypatch.setattr(importlib.util, "spec_from_file_location", spec_patch)
    assert C.non_statement_title(tmp_path / "year_end.pdf") == "Year End Statement & Summary"
    assert C.non_statement_title(tmp_path / "leaflet.pdf") == "Most Important Terms and Conditions"
    assert C.non_statement_title(tmp_path / "real.pdf") is None      # a statement is never skipped
    assert C.non_statement_title(tmp_path / "missing.pdf") is None   # unreadable is never hidden


def test_sorter_puts_non_statements_in_their_own_folder_never_unknown(tmp_path, monkeypatch):
    import importlib.util
    script = ROOT / "src" / "agents" / "skill_cc_sort" / "scripts" / "extract_sort_cc_pdfs.py"
    spec = importlib.util.spec_from_file_location("_sorter_under_test", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    reasons = {"year_end.pdf": "Year End Statement & Summary",
               "leaflet.pdf": "Most Important Terms and Conditions", "mystery.pdf": None}
    monkeypatch.setattr(C, "non_statement_title", lambda p: reasons[Path(p).name])
    assert mod.choose_folder("year_end.pdf", Path("year_end.pdf")) == ("Not", "A-Statement")
    assert mod.choose_folder("leaflet.pdf", Path("leaflet.pdf")) == ("Not", "A-Statement")
    # an unrecognised file that may be a statement stays visible as Unknown
    assert mod.choose_folder("mystery.pdf", Path("mystery.pdf")) == ("Unknown", "Unknown")
    # a recognised card keeps its folder
    assert mod.choose_folder("hsbc_premier_stmt.pdf", Path("x.pdf")) == ("HSBC", "Premier")
