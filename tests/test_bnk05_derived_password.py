"""
BNK-05 -- derive the statement password from the Entities record when the
password box is empty. Only SBM declares a rule. Synthetic entities, dummy
names and dates, a synthetic encrypted xlsx. The derived password is never
logged, written or shown.
"""
from __future__ import annotations

import datetime
import gzip
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parent
for _p in (ROOT.parent / "src", ROOT.parent / "src" / "agents", ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from agents import banks  # noqa: E402
from agents.skill_gnucash_pipeline import agent as pipe  # noqa: E402
from agents.skill_sbm import password_rule as rule  # noqa: E402
from test_bnk04_sbm_parser import _write, _rows  # noqa: E402

DERIVED = "JANE0703"       # "Jane Q Example", born 7 March -> JANE + 0703


def _prof(name="Jane Q Example", dob=datetime.date(1990, 3, 7)):
    return SimpleNamespace(name=name, dob=dob)


# ------------------------------------------------------------------ the rule

def test_rule_first_four_letters_capitals_then_ddmm():
    assert rule.derive_password(_prof()) == DERIVED
    assert rule.derive_password(_prof(dob="1990-03-07")) == DERIVED
    assert rule.derive_password(_prof(name="  jane-anne Example")) == "JANE0703"


@pytest.mark.parametrize("name,dob", [
    ("Jo Example", datetime.date(1990, 3, 7)),     # first name under 4 letters
    ("", datetime.date(1990, 3, 7)),
    ("Jane Example", None),
    ("Jane Example", ""),
    ("Jane Example", "not-a-date"),
])
def test_rule_refuses_an_incomplete_record_without_echoing_values(name, dob):   # NEGATIVE
    with pytest.raises(rule.PasswordRuleError) as ei:
        rule.derive_password(_prof(name, dob))
    msg = str(ei.value)
    assert "1990" not in msg and "JANE" not in msg.upper().replace("FIRST NAME", "")


def test_only_sbm_declares_a_rule():
    for info in banks.discover():
        has = False
        try:
            import importlib
            importlib.import_module(f"{info.package}.password_rule")
            has = True
        except ModuleNotFoundError as e:
            assert e.name == f"{info.package}.password_rule"
        assert has == (info.bank_key == "sbm"), info.bank_key


def test_derive_helper_no_rule_bank_returns_empty():                        # NEGATIVE
    hdfc = banks.get("hdfc")
    assert pipe._derive_bank_password(hdfc, _prof()) == ("", "")
    assert pipe._derive_bank_password(banks.get("sbm"), _prof())[0] == DERIVED


# ------------------------------------------------------------ end to end

_NS = 'xmlns:gnc="http://www.gnucash.org/XML/gnc" xmlns:act="http://www.gnucash.org/XML/act"'


def _acct(n, i, t, parent=None):
    s = (f'  <gnc:account version="2.0.0"><act:name>{n}</act:name>'
         f'<act:id type="guid">{i}</act:id><act:type>{t}</act:type>')
    if parent:
        s += f'<act:parent type="guid">{parent}</act:parent>'
    return s + "</gnc:account>"


def _book(tmp_path):
    xml = ('<?xml version="1.0" encoding="utf-8"?>\n' f"<gnc-v2 {_NS}>\n"
           '<gnc:book version="2.0.0">\n'
           + "\n".join([_acct("Root Account", "root", "ROOT"),
                        _acct("Assets", "a", "ASSET", "root"),
                        _acct("SBM Savings", "sbm", "BANK", "a"),
                        _acct("Expenses", "e", "EXPENSE", "root")])
           + "\n</gnc:book>\n</gnc-v2>\n")
    p = tmp_path / "book.gnucash"
    p.write_bytes(gzip.compress(xml.encode("utf-8")))
    return str(p)


def _entities(tmp_path, **ent):
    base = {"name": "Jane Q Example", "pan": "AAAAA0000A", "status": "Individual",
            "residency": "Resident", "default_regime": "new", "dob": "1990-03-07"}
    base.update(ent)
    p = tmp_path / "entities.yaml"
    p.write_text(yaml.safe_dump({"SYN": base}), encoding="utf-8")
    return str(p)


def _run(tmp_path, stmt, password=None, entity="SYN", **ent):
    out = tmp_path / "out" / "mapped.csv"
    res = pipe.run("SBM", stmt, _book(tmp_path), str(out), pdf_password=password,
                   entity=entity, entities_path=_entities(tmp_path, **ent))
    return res, out


def _no_leak(tmp_path, res, caplog, secret):
    assert secret not in res
    assert secret not in caplog.text
    for f in tmp_path.rglob("*"):
        if f.is_file() and f.suffix in (".csv", ".json", ".txt", ".log", ".yaml") \
                and f.name != "entities.yaml":
            assert secret not in f.read_text(encoding="utf-8", errors="ignore"), f


def test_derived_password_opens_the_file_and_is_never_leaked(tmp_path, caplog):
    stmt = _write(tmp_path / "s.xlsx", _rows(), password=DERIVED)
    res, out = _run(tmp_path, stmt)
    assert "password error" not in res and "did not open the file" not in res
    assert "derived from the Entities record" in res          # rule is named...
    _no_leak(tmp_path, res, caplog, DERIVED)                   # ...the value never


def test_wrong_derived_password_fails_loud_and_does_not_leak(tmp_path, caplog):   # NEGATIVE
    stmt = _write(tmp_path / "s.xlsx", _rows(), password="a-different-secret-1")
    res, out = _run(tmp_path, stmt)
    assert "❌" in res and "derived from the Entities record" in res
    assert "type the statement password" in res
    assert not out.exists()                                    # no silent empty parse
    _no_leak(tmp_path, res, caplog, DERIVED)


def test_typed_password_always_wins(tmp_path, caplog):                      # NEGATIVE
    # file opens with the typed one; the derived one would NOT open it
    stmt = _write(tmp_path / "s.xlsx", _rows(), password="typed-secret-77")
    res, _ = _run(tmp_path, stmt, password="typed-secret-77")
    assert "derived from the Entities record" not in res
    assert "password" not in res.lower().split("##")[1].split("\n")[0]
    _no_leak(tmp_path, res, caplog, "typed-secret-77")
    # and a wrong typed password is NOT rescued by the derived one
    stmt2 = _write(tmp_path / "s2.xlsx", _rows(), password=DERIVED)
    res2, _ = _run(tmp_path, stmt2, password="wrong-typed")
    assert "❌" in res2 and "derived from the Entities record" not in res2


def test_no_entity_selected_behaves_as_today(tmp_path):                     # NEGATIVE
    stmt = _write(tmp_path / "s.xlsx", _rows(), password=DERIVED)
    res, out = _run(tmp_path, stmt, entity="")
    assert "❌" in res and "derived from the Entities record" not in res
    assert not out.exists()


def test_incomplete_record_fails_loud_before_any_parse(tmp_path):           # NEGATIVE
    stmt = _write(tmp_path / "s.xlsx", _rows(), password=DERIVED)
    res, out = _run(tmp_path, stmt, dob="")
    assert "password error" in res and "date of birth" in res.lower()
    assert not out.exists()


def test_bank_without_a_rule_and_empty_box_is_untouched(tmp_path):          # NEGATIVE
    prof, _ = pipe._load_entity_profile("SYN", _entities(tmp_path))
    assert pipe._derive_bank_password(banks.get("hdfc"), prof) == ("", "")
    assert pipe._derive_bank_password(banks.get("icici"), prof) == ("", "")
