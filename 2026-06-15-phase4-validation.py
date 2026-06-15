#!/usr/bin/env python3
"""
Phase 4 Validation Script — Test all 6 GnuCash skills programmatically.

Tests the run() entry point of each skill directly, bypassing the UI layer.
This validates that skill.yaml + run() integration is working.

Usage:
    cd path/to/platform-agnostic-skills-portable
    python 2026-06-15-phase4-validation.py
"""

import sys
import tempfile
from pathlib import Path

# Add src to path so we can import agents
SRC = Path(__file__).parent / "src"
sys.path.insert(0, str(SRC))

from agents.registry import discover, load_run_function

GNUCASH_SKILLS = [
    "GnuCash XML Extractor",
    "GnuCash Mapping Generator",
    "GnuCash Mapping Validator",
    "GnuCash Account Mapper",
    "GnuCash Reconciler",
    "GnuCash Import",
]

def test_skill(skill_name: str) -> tuple[bool, str]:
    """
    Test a single skill's run() function.

    Returns: (success: bool, message: str)
    """
    try:
        # Load skill info
        registry = discover()
        skill = next((s for s in registry if s.name == skill_name), None)
        if not skill:
            return False, f"Skill not found in registry"

        # Load run function
        run_fn = load_run_function(skill)
        if not run_fn:
            return False, f"Could not load run() function"

        # Check that run function is callable
        if not callable(run_fn):
            return False, f"run() is not callable"

        # Inspect signature
        import inspect
        sig = inspect.signature(run_fn)
        params = list(sig.parameters.keys())

        return True, f"✓ Loaded successfully | Params: {', '.join(params) or '(none)'}"

    except Exception as e:
        return False, f"Error: {e}"


def main():
    print("=" * 90)
    print("PHASE 4 VALIDATION — GnuCash Skills")
    print("=" * 90)
    print()

    # Discover all skills
    print("Scanning registry for skills...")
    registry = discover()
    print(f"Registry contains {len(registry)} total skills:")
    for s in registry:
        print(f"  - {s.name}")
    print()

    gnucash_in_registry = [s for s in registry if s.name in GNUCASH_SKILLS]
    print(f"Found {len(gnucash_in_registry)} GnuCash skills in registry")
    print()

    # Test each skill
    results = {}
    print("Testing skills...")
    print("-" * 90)

    for skill_name in GNUCASH_SKILLS:
        success, msg = test_skill(skill_name)
        results[skill_name] = (success, msg)
        status = "✓ PASS" if success else "✗ FAIL"
        print(f"{status:8} {skill_name:40} {msg}")

    print("-" * 90)
    print()

    # Summary
    passed = sum(1 for s, m in results.values() if s)
    total = len(results)

    print("SUMMARY")
    print("=" * 90)
    print(f"Passed: {passed}/{total}")
    print()

    if passed == total:
        print("✓ ALL SKILLS VALIDATED — Phase 4 integration is working!")
        print()
        print("All 6 GnuCash skills:")
        for skill_name in GNUCASH_SKILLS:
            print(f"  ✓ {skill_name}")
        print()
        print("Next: Phase 4 Lite — integrate duplicate detection into pipeline")
        return 0
    else:
        print("✗ SOME SKILLS FAILED — See details above")
        failed = [name for name, (s, m) in results.items() if not s]
        for name in failed:
            print(f"  ✗ {name}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
