"""
Runtime hook for PyInstaller — ensures agents/ data files are importable.

The agents/ tree is bundled as raw .py data files (via the datas line in
paskills.spec) into _MEIPASS/agents/.  They are NOT compiled as hidden
imports because PyInstaller's static analysis chokes on the transitive
langgraph imports in base_agent.py.

This hook guarantees _MEIPASS is at the front of sys.path so that
`import agents.registry` (and every other agents.* import) resolves
to the data-directory .py files.
"""
import sys
import os

meipass = getattr(sys, '_MEIPASS', None)
if meipass:
    # Ensure _MEIPASS is the FIRST entry so the agents/ data tree wins.
    if meipass in sys.path:
        sys.path.remove(meipass)
    sys.path.insert(0, meipass)

    # If something already imported a partial 'agents' package, nuke it.
    for key in list(sys.modules):
        if key == 'agents' or key.startswith('agents.'):
            del sys.modules[key]
