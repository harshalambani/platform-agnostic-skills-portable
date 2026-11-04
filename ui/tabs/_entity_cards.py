"""
ui/tabs/_entity_cards.py -- the "Credit cards" sub-screen of the Entities tab (CC-28).

Edits <data_root>/settings/cc_cards.yaml, the one card list every credit-card skill
reads (see agents/cc_cards.py for the format and the strict loader). It writes that
file only; entities.yaml is read (for the holder dropdown) and never written.

Save discipline, same as the Entities screen:
  - the path is anchored with ui._config.data_root_dir()
  - the new text is validated with the SAME loader the skills use, before any write
  - a timestamped .bak of the existing file is written before every save
  - the file's top comment block is kept verbatim; every text value is written
    double-quoted
  - a file the loader refuses (an unknown key, an unquoted YES, a full number) is NOT
    overwritten: the screen shows the loader's message and the file is left as it is
  - an error message never echoes what was typed in the last4 field
"""
from __future__ import annotations

import datetime
import os
import re
import shutil
from pathlib import Path

import gradio as gr

from .. import _config as _config_mod

_NEW = "__new__"
_LAST4_RX = re.compile(r"[0-9]{4}")


def _cc():
    from agents import cc_cards  # noqa: PLC0415
    return cc_cards


def _settings_config_path() -> Path:
    return _config_mod.data_root_dir() / "settings" / "config.yaml"


def cards_file() -> Path:
    return _cc().cards_path_for(_settings_config_path())


def _entities_file() -> Path:
    return _cc().entities_path_for(_settings_config_path())


def _holder_keys() -> list[str]:
    return sorted(_cc().entity_keys_from(_entities_file()))


# ---------------------------------------------------------------------------
# Pure logic (tested without Gradio)
# ---------------------------------------------------------------------------

def parse_last4_text(text: str) -> list[str]:
    """'1234, 5678' -> ['1234', '5678']. Raises ValueError with a message that never
    echoes the input: anything that is not exactly 4 digits per entry (a full card
    number, 3 or 5 digits, letters) is refused."""
    parts = [p for p in re.split(r"[\s,;]+", (text or "").strip()) if p]
    if not parts:
        raise ValueError("Enter the card's last 4 digits (exactly 4 digits; two numbers can be separated by a comma).")
    for p in parts:
        if not _LAST4_RX.fullmatch(p):
            raise ValueError("Each last-4 entry must be exactly 4 digits. A full card number is not accepted "
                             "and was not saved.")
    return parts


def _as_rows(cards) -> list[dict]:
    return [{"bank": c.bank, "card": c.card, "last4": list(c.last4), "holder": c.holder, "active": c.active,
             "account": getattr(c, "account", "") or ""}
            for c in cards]


def read_top(path: Path, entity_keys) -> tuple[str, str]:
    """(ccp_split_from as ISO text or "", prepayment_account or ""). Empty when the file is
    missing or not usable (read_current reports that)."""
    cc = _cc()
    try:
        cards = cc.parse_text(Path(path).read_text(encoding="utf-8"), entity_keys)
    except (OSError, UnicodeDecodeError, cc.CardListError):
        return "", ""
    return (cards.split_from.isoformat() if cards.split_from else ""), cards.prepayment_account


def read_current(path: Path, entity_keys) -> tuple[list[dict], str | None, str | None]:
    """(rows, header_text, error). A missing file is an empty list with the standard
    header and no error (the first save creates it). A file the loader refuses gives
    error text and no rows: it must be fixed before the screen edits it."""
    cc = _cc()
    if not Path(path).is_file():
        return [], cc.TOP_COMMENT, None
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return [], None, f"{cc.CARDS_FILENAME} could not be read."
    try:
        cards = cc.parse_text(text, entity_keys)
    except cc.CardListError as exc:
        return [], None, f"{cc.CARDS_FILENAME} is not usable as it stands: {exc}. Nothing was changed."
    return _as_rows(cards), (cc.leading_comments(text) or cc.TOP_COMMENT), None


def _write_checked(path: Path, rows: list[dict], header: str, entity_keys, top: tuple | None = None) -> str:
    """Validate the would-be file with the shared loader, back up, write. Returns a
    status line; raises cc_cards.CardListError (nothing written) when it is refused."""
    cc = _cc()
    if top is None:                           # keep the two top-level settings the file already has
        top = read_top(path, entity_keys)
    text = cc.render_yaml(rows, header, top[0], top[1])
    cc.parse_text(text, entity_keys)          # the same strict loader the skills use
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    backup_msg = "No earlier file, nothing to back up."
    if path.is_file():
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(f"{path.name}.bak-{stamp}")
        n = 1
        while backup.exists():
            n += 1
            backup = path.with_name(f"{path.name}.bak-{stamp}-{n}")
        shutil.copy2(path, backup)
        backup_msg = f"Backup written: {backup.name}"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)
    return backup_msg


def save_card(path: Path, entity_keys, index: int | None, folder_label: str, last4_text: str,
              holder: str, active: bool, account: str | None = None) -> str:
    """Add (index None) or change one card. Returns a Markdown status. `account` is the
    card's CC-39 sub-account; None keeps what the card already has (a new card has none)."""
    cc = _cc()
    rows, header, err = read_current(path, entity_keys)
    if err:
        return f"**Not saved.** {err}"
    try:
        digits = parse_last4_text(last4_text)
    except ValueError as exc:
        return f"**Not saved.** {exc}"
    bank, _, card = (folder_label or "").partition("-")
    folder = cc.folder_for(bank, card)
    if folder is None:
        return "**Not saved.** Pick the bank and card from the list."
    row = {"bank": folder[0], "card": folder[1], "last4": digits, "holder": holder or "", "active": bool(active),
           "account": (account or "").strip() if account is not None else ""}
    if index is None:
        rows.append(row)
    else:
        if not 0 <= index < len(rows):
            return "**Not saved.** That card is no longer in the list; refresh and try again."
        if account is None:
            row["account"] = rows[index].get("account", "")
        rows[index] = row
    try:
        backup_msg = _write_checked(path, rows, header, entity_keys)
    except cc.CardListError as exc:
        return f"**Not saved.** {exc}"
    except OSError:
        return "**Not saved.** The file could not be written."
    return f"**Saved**\n\n{backup_msg}"


def save_top(path: Path, entity_keys, split_from: str, prepayment_account: str) -> str:
    """Set the two top-level settings (CC-39 ccp_split_from, CC-29 prepayment_account); the
    cards are written back unchanged. Blank clears a setting."""
    cc = _cc()
    rows, header, err = read_current(path, entity_keys)
    if err:
        return f"**Not saved.** {err}"
    top = ((split_from or "").strip(), (prepayment_account or "").strip())
    try:
        backup_msg = _write_checked(path, rows, header, entity_keys, top)
    except cc.CardListError as exc:
        return f"**Not saved.** {exc}"
    except OSError:
        return "**Not saved.** The file could not be written."
    return f"**Saved**\n\n{backup_msg}"


def delete_card(path: Path, entity_keys, index: int | None, confirmed: bool) -> str:
    cc = _cc()
    if index is None:
        return "**Not deleted.** Pick a card first."
    if not confirmed:
        return "**Not deleted.** Tick the confirmation box first."
    rows, header, err = read_current(path, entity_keys)
    if err:
        return f"**Not deleted.** {err}"
    if not 0 <= index < len(rows):
        return "**Not deleted.** That card is no longer in the list; refresh and try again."
    del rows[index]
    try:
        backup_msg = _write_checked(path, rows, header, entity_keys)
    except cc.CardListError as exc:
        return f"**Not deleted.** {exc}"
    except OSError:
        return "**Not deleted.** The file could not be written."
    return f"**Deleted**\n\n{backup_msg}"


def grouped_markdown(rows: list[dict]) -> str:
    """The list, grouped by holder (blank holder last)."""
    if not rows:
        return "_No cards listed yet._"
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r["holder"] or "", []).append(r)
    out = []
    for holder in sorted(groups, key=lambda h: (h == "", h)):
        out.append(f"**{holder or '(holder not set)'}**")
        for r in groups[holder]:
            state = "" if r["active"] else " (not active)"
            acct = f", sub-account {r['account']}" if r.get("account") else ""
            out.append(f"- {r['bank']}-{r['card']}: ending {', '.join(r['last4'])}{state}{acct}")
        out.append("")
    return "\n".join(out)


def _card_choices(rows: list[dict]) -> list[tuple[str, str]]:
    out = [("(new card)", _NEW)]
    for i, r in enumerate(rows):
        out.append((f"{r['holder'] or '(no holder)'} - {r['bank']}-{r['card']} ending {', '.join(r['last4'])}", str(i)))
    return out


# ---------------------------------------------------------------------------
# Gradio
# ---------------------------------------------------------------------------

def _folder_choices() -> list[str]:
    return [f"{b}-{c}" for b, c in _cc().KNOWN_FOLDERS]


def _snapshot():
    rows, _hdr, err = read_current(cards_file(), set(_holder_keys()))
    return rows, err


def _refresh():
    rows, err = _snapshot()
    keys = _holder_keys()
    shown = (f"**{err}**" if err else grouped_markdown(rows))
    sf, pa = read_top(cards_file(), set(keys))
    return (shown, gr.update(choices=_card_choices(rows), value=_NEW),
            gr.update(choices=[("(not set)", "")] + [(k, k) for k in keys], value=""),
            sf, pa)


def _pick(choice):
    rows, _err = _snapshot()
    if choice in (None, "", _NEW) or not str(choice).isdigit() or int(choice) >= len(rows):
        return gr.update(value=None), "", gr.update(value=""), True, ""
    r = rows[int(choice)]
    return (gr.update(value=f"{r['bank']}-{r['card']}"), ", ".join(r["last4"]),
            gr.update(value=r["holder"]), r["active"], r.get("account", ""))


def _on_save(choice, folder_label, last4_text, holder, active, account):
    index = None if choice in (None, "", _NEW) else int(choice)
    msg = save_card(cards_file(), set(_holder_keys()), index, folder_label, last4_text, holder, active,
                    account or "")
    return (msg, *_refresh())


def _on_save_top(split_from, prepayment_account):
    msg = save_top(cards_file(), set(_holder_keys()), split_from or "", prepayment_account or "")
    return (msg, *_refresh())


def _on_delete(choice, confirmed):
    index = None if choice in (None, "", _NEW) else int(choice)
    msg = delete_card(cards_file(), set(_holder_keys()), index, confirmed)
    return (msg, *_refresh(), False)


def render(container_tab=None) -> None:
    """The sub-screen; call it inside an Accordion or Group on the Entities tab."""
    gr.Markdown(
        "Every credit-card skill reads this one list. Enter only the **last 4 digits** of a card "
        "(a card with two numbers on one bill takes both, separated by a comma); a full card number "
        "is refused. Saving writes `settings/cc_cards.yaml` only, after a timestamped backup."
    )
    list_md = gr.Markdown("_Open this tab to load the list._")
    status = gr.Markdown("")
    card_dd = gr.Dropdown(label="Card", choices=[("(new card)", _NEW)], value=_NEW, interactive=True)
    with gr.Row():
        folder_dd = gr.Dropdown(label="Bank and card", choices=_folder_choices(), value=None, interactive=True)
        holder_dd = gr.Dropdown(label="Holder (entity)", choices=[("(not set)", "")], value="", interactive=True)
    with gr.Row():
        last4_tb = gr.Textbox(label="Last 4 digits", placeholder="four digits, e.g. 1234", max_length=24)
        active_cb = gr.Checkbox(label="Active", value=True)
    with gr.Row():
        save_btn = gr.Button("Save card", variant="primary")
        refresh_btn = gr.Button("Refresh", scale=0, min_width=100)
    confirm_cb = gr.Checkbox(label="Yes, delete the selected card from the list", value=False)
    delete_btn = gr.Button("Delete selected card", variant="stop")

    account_tb = gr.Textbox(
        label="Card sub-account (optional)",
        placeholder="Expense:Withdrawals:Credit Card Payment:<card>; blank = the card is not split",
        max_length=200)
    gr.Markdown("**Credit Card Payment split** (applies to the whole list). Checked again against the book "
                "whenever Book Spends runs; a hidden, placeholder or dormant account is refused there.")
    with gr.Row():
        split_tb = gr.Textbox(label="Split from date (ccp_split_from)", placeholder="YYYY-MM-DD; blank = no split",
                              max_length=10)
        prepay_tb = gr.Textbox(label="Corporate card prepayment account (prepayment_account)",
                               placeholder="a Drawings account; blank = none", max_length=200)
    save_top_btn = gr.Button("Save split settings")

    outs = [list_md, card_dd, holder_dd, split_tb, prepay_tb]
    card_dd.change(fn=_pick, inputs=[card_dd], outputs=[folder_dd, last4_tb, holder_dd, active_cb, account_tb])
    save_btn.click(fn=_on_save, inputs=[card_dd, folder_dd, last4_tb, holder_dd, active_cb, account_tb],
                   outputs=[status, *outs])
    save_top_btn.click(fn=_on_save_top, inputs=[split_tb, prepay_tb], outputs=[status, *outs])
    refresh_btn.click(fn=_refresh, inputs=[], outputs=outs)
    delete_btn.click(fn=_on_delete, inputs=[card_dd, confirm_cb], outputs=[status, *outs, confirm_cb])
    if container_tab is not None:
        container_tab.select(fn=_refresh, inputs=[], outputs=outs)
