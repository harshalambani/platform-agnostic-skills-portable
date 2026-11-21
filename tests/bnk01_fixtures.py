"""BNK-01: one synthetic statement per bank, parsed through the registry.

Used by test_bnk01_nothing_learned_breaks.py, whose golden file was produced by
running ``dump_rows()`` on the code as it was BEFORE BNK-01 (origin/main
e522f95). Importing this module touches nothing but the fixtures and the bank
registry, so it runs on the old and the new code alike.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT / "src", ROOT / "tests", ROOT / "tests" / "skill_bob",
          ROOT / "tests" / "skill_hdfc", ROOT / "tests" / "skill_hsbc",
          ROOT / "tests" / "skill_icici", ROOT / "tests" / "skill_kotak"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


def build_all(tmp: Path) -> dict[str, tuple[Path, str | None]]:
    """bank_key -> (file, password) for one synthetic statement each."""
    import bob_fixture_gen
    import hdfc_fixture_gen
    import hsbc_fixture_gen
    import icici_fixture_gen
    import kotak_fixture_gen
    import test_bnk04_sbm_parser as sbm_t

    out: dict[str, tuple[Path, str | None]] = {}
    f = tmp / "bob.pdf"
    f.write_bytes(bob_fixture_gen.build_pdf())
    out["bob"] = (f, None)
    f = tmp / "hdfc.csv"
    f.write_text(hdfc_fixture_gen.build_csv_text(), encoding="utf-8")
    out["hdfc"] = (f, None)
    f = tmp / "hsbc.xlsx"
    f.write_bytes(hsbc_fixture_gen.build_xlsx())
    out["hsbc"] = (f, None)
    f = tmp / "icici.xls"
    f.write_bytes(icici_fixture_gen.build_xls())
    out["icici"] = (f, None)
    f = tmp / "kotak.pdf"
    f.write_bytes(kotak_fixture_gen.build_pdf(layout="real"))
    out["kotak"] = (f, None)
    f = tmp / "sbm.xlsx"
    sbm_t._write(f, sbm_t._rows())
    out["sbm"] = (f, sbm_t.PW)
    return out


def dump_rows() -> dict[str, list[dict]]:
    """The canonical rows each bank's reader gives for its fixture."""
    from agents import banks

    result: dict[str, list[dict]] = {}
    with tempfile.TemporaryDirectory(prefix="bnk01-fx-", ignore_cleanup_errors=True) as td:
        for key, (path, pw) in build_all(Path(td)).items():
            skill = banks.load_bank_skill(banks.get(key))
            result[key] = [dict(r) for r in skill.parse(path, password=pw).rows]
    return result
