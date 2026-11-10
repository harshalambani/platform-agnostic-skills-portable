"""Licence gate: the classifier catches every GPL-family spelling, passes the
permissive ones, and the real lock is clean."""
from __future__ import annotations

import sys
from importlib import metadata
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import license_gate as lg  # noqa: E402


class FakeMD(dict):
    def __init__(self, license_=None, expr=None, classifiers=()):
        super().__init__()
        if license_ is not None:
            self["License"] = license_
        if expr is not None:
            self["License-Expression"] = expr
        self._c = list(classifiers)

    def get_all(self, key):
        return self._c if key == "Classifier" else None


@pytest.mark.parametrize("text", [
    "GPL", "GPL-3.0", "GPL-3.0-or-later", "GPLv3", "GPLv2+", "GPL 2 or later",
    "LGPL", "LGPLv3", "LGPL-2.1-only", "LGPLv3+", "AGPL", "AGPL-3.0", "AGPLv3",
    "GNU General Public License v3", "GNU Lesser General Public License v3 (LGPLv3)",
    "GNU Library or Lesser General Public License (LGPL)",
    "GNU Affero General Public License v3", "gnu   general public   licence",
    "MIT OR GPL-2.0", "Apache-2.0 / LGPL-2.1", "BSD or GPL",
])
def test_license_field_flagged(text):
    assert lg.classify_license(license_field=text), text


@pytest.mark.parametrize("text", [
    "GPL-3.0-only", "LGPL-3.0-or-later", "MIT OR AGPL-3.0-only",
    "GNU Lesser General Public License",
])
def test_expression_flagged(text):
    assert lg.classify_license(license_expression=text), text


@pytest.mark.parametrize("c", [
    "License :: OSI Approved :: GNU General Public License v3 (GPLv3)",
    "License :: OSI Approved :: GNU Lesser General Public License v3 (LGPLv3)",
    "License :: OSI Approved :: GNU Affero General Public License v3",
    "License :: OSI Approved :: GNU Library or Lesser General Public License (LGPL)",
    "License :: OSI Approved :: GNU General Public License v2 or later (GPLv2+)",
])
def test_classifier_flagged(c):
    assert lg.classify_license(classifiers=["Programming Language :: Python", c])


@pytest.mark.parametrize("text", [
    "MIT", "MIT License", "BSD", "BSD-3-Clause", "BSD License", "Apache-2.0",
    "Apache Software License", "ISC", "PSF-2.0", "MPL-2.0", "Unlicense",
    "MIT AND Apache-2.0", "", None,
])
def test_permissive_passes(text):
    assert lg.classify_license(license_field=text) == []
    assert lg.classify_license(license_expression=text) == []


def test_permissive_classifiers_pass():
    assert lg.classify_license(classifiers=[
        "License :: OSI Approved :: MIT License",
        "License :: OSI Approved :: BSD License",
        "License :: OSI Approved :: Apache Software License",
        "Topic :: Software Development :: Libraries",
    ]) == []


def test_long_permissive_text_mentioning_gnu_late_is_not_flagged():
    text = "MIT License\n\nPermission is hereby granted ...\n" + ("x " * 400) + \
        "\nThis is not licensed under the GNU General Public License."
    assert lg.classify_license(license_field=text) == []


def test_long_copyleft_text_is_flagged_by_its_head():
    text = "GNU GENERAL PUBLIC LICENSE\nVersion 3, 29 June 2007\n" + ("x " * 400)
    assert lg.classify_license(license_field=text)


def test_non_license_classifier_text_ignored():
    assert lg.classify_license(classifiers=["Topic :: Utilities :: gpl tools"]) == []


def test_check_installed_flags_and_lists_absent():
    table = {
        "good": FakeMD("MIT"),
        "bad": FakeMD("GPL-3.0"),
        "worse": FakeMD(classifiers=["License :: OSI Approved :: GNU Affero General Public License v3"]),
    }

    def lookup(name):
        if name not in table:
            raise metadata.PackageNotFoundError(name)
        return table[name]

    violations, absent = lg.check_installed(["good", "bad", "worse", "gone"], lookup)
    assert sorted(violations) == ["bad", "worse"]
    assert absent == ["gone"]


def test_main_fails_on_flagged_package(tmp_path, monkeypatch, capsys):
    lock = tmp_path / "lock.txt"
    lock.write_text("badpkg==1.0 \\\n    --hash=sha256:00\n", encoding="utf-8")
    monkeypatch.setattr(metadata, "metadata", lambda n: FakeMD("LGPLv3"))
    assert lg.main(["--lock", str(lock)]) == 1
    assert "badpkg" in capsys.readouterr().out


def test_main_fails_on_unexpected_absent_package(tmp_path, monkeypatch):
    lock = tmp_path / "lock.txt"
    lock.write_text("ghost==1.0 \\\n    --hash=sha256:00\n", encoding="utf-8")

    def lookup(n):
        raise metadata.PackageNotFoundError(n)

    monkeypatch.setattr(metadata, "metadata", lookup)
    assert lg.main(["--lock", str(lock)]) == 1


def test_main_passes_when_only_expected_absent(tmp_path, monkeypatch):
    lock = tmp_path / "lock.txt"
    lock.write_text(
        "okpkg==1.0 \\\n    --hash=sha256:00\npywebview==5.0 \\\n    --hash=sha256:00\n",
        encoding="utf-8")

    def lookup(n):
        if n == "pywebview":
            raise metadata.PackageNotFoundError(n)
        return FakeMD("MIT")

    monkeypatch.setattr(metadata, "metadata", lookup)
    assert lg.main(["--lock", str(lock)]) == 0


def test_lock_names_reads_hashed_lock():
    text = "alpha==1.0 \\\n    --hash=sha256:aa\nBeta_pkg==2 \\\n    --hash=sha256:bb\n# c\n"
    assert lg.lock_names(text) == ["alpha", "Beta_pkg"]


def test_real_lock_has_no_gpl_family_package():
    lock = ROOT / "requirements-lock.txt"
    violations, absent = lg.check_installed(lg.lock_names(lock.read_text(encoding="utf-8")))
    assert violations == {}
    assert set(absent) <= lg.EXPECTED_ABSENT


def test_extract_msg_chain_is_not_in_the_lock():
    names = {n.lower().replace("_", "-") for n in
             lg.lock_names((ROOT / "requirements-lock.txt").read_text(encoding="utf-8"))}
    assert not names & {"extract-msg", "pcodedmp", "rtfde"}


def test_packages_the_app_imports_were_not_only_there_via_the_removed_chain():
    # bs4 (parse_eguile.py) used to arrive only through extract-msg; removing
    # that left an import with no declaration and broke every ITR test in CI.
    reqs = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    lock = (ROOT / "requirements-lock.txt").read_text(encoding="utf-8").lower()
    assert "beautifulsoup4" in reqs
    assert "beautifulsoup4==" in lock and "soupsieve==" in lock
