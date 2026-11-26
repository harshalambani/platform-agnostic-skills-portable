"""Switch LangChain / LangSmith tracing off for every run of the app.

Imported first thing by ui/webui.py, before gradio or any langchain module is
imported. The app never wants run data sent to a tracing service, whatever the
user's environment says, so the two switches are set to "false" here.
"""
from __future__ import annotations

import os

SWITCHES = ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2")


def force_tracing_off() -> None:
    for name in SWITCHES:
        os.environ[name] = "false"


force_tracing_off()
