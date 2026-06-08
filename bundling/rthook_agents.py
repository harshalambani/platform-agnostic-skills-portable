"""
Runtime hook for PyInstaller — ensures agents/ data files are importable.

PyInstaller may fail to compile agents.base_agent and agents.registry as
hidden imports (due to complex transitive imports from langgraph at analysis
time). The raw .py files ARE bundled via the datas line, but sit in
_MEIPASS/agents/ which may not be importable if the compiled 'agents'
package shadows the data directory.

This hook removes any partial 'agents' entry from sys.modules so that
Python re-discovers the package from the data directory on first import.
"""
import sys
import os

# In a --onedir build, _MEIPASS points to _internal/.
# The agents/ data tree lives at _MEIPASS/agents/.
# Ensure _MEIPASS is early in sys.path (it usually is, but be safe).
meipass = getattr(sys, '_MEIPASS', None)
if meipass and meipass not in sys.path:
    sys.path.insert(0, meipass)

# If PyInstaller partially compiled the 'agents' package (just __init__)
# but not its sub-modules, remove it so Python re-discovers from disk.
for key in list(sys.modules):
    if key == 'agents' or key.startswith('agents.'):
        del sys.modules[key]
