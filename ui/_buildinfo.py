"""
ui/_buildinfo.py — overwritten by packaging/build.py on each build.

In source-mode runs, both fields fall back to "dev". The build script rewrites
this file just before running PyInstaller so the frozen executable carries
deterministic version + commit metadata, surfaced in the UI's About panel.
"""

VERSION: str = "dev"
COMMIT_SHA: str = "dev"
BUILD_DIRTY: bool = True
BUILD_TIMESTAMP: str = "dev"
