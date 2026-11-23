"""PERF-04 C2: build the heavy GnuCash sub-tab groups AFTER the page is drawn.

The launched app draws its page with each heavy group (Banks, Partner,
Inter-entity, 26AS, KRChoksey, ITR, Entities) still empty, then builds them in
the background, one after another, from the page-load event. Each group is one
``gr.render`` triggered by ``app.load`` only: it runs once per page load and
never on a later tab click, so nothing is rebuilt and no input is reset.

The groups share one concurrency id with a limit of 1, so the queue runs them
strictly one at a time, in the order they were declared. One group finishing
never holds up the page being drawn (the page is drawn before any starts).

Unlaunched builds (tests, build_app(launch=False)) stay eager.
"""
from __future__ import annotations

import threading

import gradio as gr

# One queue slot for every deferred group -> one group at a time, in order.
CONCURRENCY_ID = "pa-lazy-tab-groups"

# Every deferred build, in the order it ran: (label,). Tests read this to prove
# a group is built exactly once per page load. Cleared by reset_build_log().
_build_log: list[str] = []
_log_lock = threading.Lock()


def reset_build_log() -> None:
    with _log_lock:
        _build_log.clear()


def build_log() -> list[str]:
    with _log_lock:
        return list(_build_log)


def register_group(app, tab, label: str, body) -> None:
    """Defer ``body(tab)`` until the page has loaded. Call inside ``with tab:``.

    A factory (not a loop body) so ``label``/``body``/``tab`` are bound here,
    at definition: a deferred body never sees a later loop iteration's values.
    """
    from ui.tabs import _generic  # noqa: PLC0415

    @gr.render(
        triggers=[app.load],
        concurrency_limit=1,
        concurrency_id=CONCURRENCY_ID,
        trigger_mode="once",
    )
    def _build_group():
        with _log_lock:
            _build_log.append(label)
        with _generic.lazy_group_scope() as dropdowns:
            body(tab)
        # The page-wide model fill (wire_deferred_model_loads) ran against the
        # dropdowns that existed at build time; these are new, so they were
        # created already filled and only need the focus refresh.
        _generic.wire_model_focus(dropdowns)
