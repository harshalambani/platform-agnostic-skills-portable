"""
ui/tabs/settings.py — Settings tab for managing LLM endpoints.

Provides a Gradio UI for viewing, switching, adding, editing, deleting,
and testing LLM endpoint configurations. Reads and writes through
ui._config (load_portable_config / write_portable_config) so the user
never needs to hand-edit config.yaml.

Supported providers: "ollama", "openai_compatible".
"""
from __future__ import annotations

import gradio as gr

from .. import _config
from .. import _health


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------

def _load_endpoint_names() -> list[str]:
    """Return sorted list of endpoint names from the config."""
    cfg = _config.load_portable_config()
    return sorted((cfg.get("endpoints") or {}).keys())


def _load_active() -> str:
    """Return the name of the currently active endpoint."""
    return _config.active_endpoint_name()


def _load_endpoint(name: str) -> dict:
    """Return the endpoint dict for a given name, or empty dict."""
    cfg = _config.load_portable_config()
    return (cfg.get("endpoints") or {}).get(name, {})


def _test_endpoint(name: str) -> str:
    """Test connectivity of the named endpoint, return a status string."""
    ep = _load_endpoint(name)
    if not ep:
        return f"Endpoint '{name}' not found in config."
    result = _health.check(ep)
    if result.ok:
        models = ", ".join(result.models[:10]) if result.models else "(none listed)"
        suffix = f"… and {len(result.models) - 10} more" if len(result.models) > 10 else ""
        return f"**Connected** — {result.detail}\n\nModels: {models}{suffix}"
    return f"**{result.status.upper()}** — {result.detail}"


def _save_endpoint(
    name: str,
    provider: str,
    base_url: str,
    default_model: str,
    api_key: str,
    temperature: float,
    set_active: bool,
) -> str:
    """Save or update an endpoint in the config. Returns a status message."""
    name = name.strip()
    if not name:
        return "Error: endpoint name cannot be empty."
    if not base_url.strip():
        return "Error: base URL cannot be empty."
    if provider not in ("ollama", "openai_compatible"):
        return "Error: provider must be 'ollama' or 'openai_compatible'."

    cfg = _config.load_portable_config()
    if "endpoints" not in cfg or cfg["endpoints"] is None:
        cfg["endpoints"] = {}

    ep: dict = {
        "provider": provider,
        "base_url": base_url.strip().rstrip("/"),
        "default_model": default_model.strip(),
        "temperature": round(temperature, 2),
    }
    if provider == "openai_compatible":
        ep["api_key"] = api_key.strip() or "not-needed"

    cfg["endpoints"][name] = ep
    if set_active:
        cfg["active_endpoint"] = name

    _config.write_portable_config(cfg)
    action = "Updated" if name in (cfg.get("endpoints") or {}) else "Added"
    active_note = " (set as active)" if set_active else ""
    return f"**{action}** endpoint `{name}`{active_note}."


def _delete_endpoint(name: str) -> str:
    """Delete an endpoint from the config. Returns a status message."""
    name = name.strip()
    cfg = _config.load_portable_config()
    endpoints = cfg.get("endpoints") or {}
    if name not in endpoints:
        return f"Error: endpoint '{name}' not found."
    if len(endpoints) <= 1:
        return "Error: cannot delete the last remaining endpoint."
    del endpoints[name]
    # If we deleted the active endpoint, switch to the first remaining one.
    if cfg.get("active_endpoint") == name:
        cfg["active_endpoint"] = next(iter(endpoints))
    _config.write_portable_config(cfg)
    return f"**Deleted** endpoint `{name}`. Active is now `{cfg['active_endpoint']}`."


def _switch_active(name: str) -> str:
    """Switch the active endpoint. Returns a status message."""
    name = name.strip()
    cfg = _config.load_portable_config()
    endpoints = cfg.get("endpoints") or {}
    if name not in endpoints:
        return f"Error: endpoint '{name}' not found."
    cfg["active_endpoint"] = name
    _config.write_portable_config(cfg)
    return f"Active endpoint switched to `{name}`."


# ---------------------------------------------------------------------------
# Public: render the Settings tab.
# ---------------------------------------------------------------------------

def render() -> None:
    """
    Render the complete Settings tab body.

    Must be called inside a `with gr.Tab(...)` context.
    """
    gr.Markdown(
        "## Settings\n\n"
        "Manage LLM endpoints and test connectivity. "
        "Changes are saved to `config.yaml` immediately."
    )

    # -- Current config info --
    config_path_display = str(_config.PORTABLE_CONFIG_PATH)
    gr.Markdown(
        f"**Config file:** `{config_path_display}`\n\n"
        f"**Output directory:** `{_config.output_dir()}`"
    )

    # -----------------------------------------------------------------------
    # Section 1: Active endpoint selector.
    # -----------------------------------------------------------------------
    gr.Markdown("### Active Endpoint")

    with gr.Row():
        active_dd = gr.Dropdown(
            label="Active endpoint",
            choices=_load_endpoint_names(),
            value=_load_active(),
            interactive=True,
        )
        switch_btn = gr.Button("Switch", variant="secondary")
        test_btn = gr.Button("Test Connection", variant="secondary")

    status_md = gr.Markdown("_Select an endpoint and click Test Connection._")

    # Switch active endpoint.
    def _on_switch(name):
        msg = _switch_active(name)
        return msg

    switch_btn.click(fn=_on_switch, inputs=[active_dd], outputs=[status_md])

    # Test connectivity.
    def _on_test(name):
        return _test_endpoint(name)

    test_btn.click(fn=_on_test, inputs=[active_dd], outputs=[status_md])

    # -----------------------------------------------------------------------
    # Section 2: View / edit endpoint details.
    # -----------------------------------------------------------------------
    gr.Markdown("### Endpoint Details")
    gr.Markdown(
        "_Select an endpoint above to load its details, or type a new name "
        "to create one._"
    )

    with gr.Row():
        with gr.Column(scale=1):
            ep_name = gr.Textbox(
                label="Endpoint name",
                placeholder="e.g. local_ollama",
                interactive=True,
            )
            ep_provider = gr.Dropdown(
                label="Provider",
                choices=["ollama", "openai_compatible"],
                value="ollama",
                interactive=True,
            )
            ep_base_url = gr.Textbox(
                label="Base URL",
                placeholder="http://localhost:11434",
                interactive=True,
            )
            ep_default_model = gr.Textbox(
                label="Default model",
                placeholder="e.g. gemma4, llama3.1, qwen3",
                interactive=True,
            )
            ep_api_key = gr.Textbox(
                label="API key (openai_compatible only)",
                placeholder="not-needed",
                interactive=True,
                visible=True,
            )
            ep_temperature = gr.Slider(
                label="Temperature",
                minimum=0.0,
                maximum=2.0,
                step=0.05,
                value=0.0,
                interactive=True,
            )
            ep_set_active = gr.Checkbox(
                label="Set as active endpoint after saving",
                value=False,
            )

        with gr.Column(scale=1):
            save_status = gr.Markdown("_Edit fields and click Save._")
            with gr.Row():
                save_btn = gr.Button("Save Endpoint", variant="primary")
                delete_btn = gr.Button("Delete Endpoint", variant="stop")

    # Load endpoint details when dropdown selection changes.
    def _on_select_endpoint(name):
        ep = _load_endpoint(name)
        if not ep:
            return (
                name,                           # ep_name
                "ollama",                        # ep_provider
                "",                              # ep_base_url
                "",                              # ep_default_model
                "",                              # ep_api_key
                0.0,                             # ep_temperature
                False,                           # ep_set_active
                f"_No endpoint named `{name}`._",  # save_status
            )
        return (
            name,
            ep.get("provider", "ollama"),
            ep.get("base_url", ""),
            ep.get("default_model", ""),
            ep.get("api_key", ""),
            float(ep.get("temperature", 0.0)),
            False,
            f"_Loaded `{name}`. Edit and Save, or Delete._",
        )

    active_dd.change(
        fn=_on_select_endpoint,
        inputs=[active_dd],
        outputs=[
            ep_name, ep_provider, ep_base_url, ep_default_model,
            ep_api_key, ep_temperature, ep_set_active, save_status,
        ],
    )

    # Save endpoint.
    def _on_save(name, provider, base_url, default_model, api_key, temperature, set_active):
        msg = _save_endpoint(name, provider, base_url, default_model, api_key, temperature, set_active)
        # Refresh the dropdown choices and active value.
        new_names = _load_endpoint_names()
        new_active = _load_active()
        return msg, gr.update(choices=new_names, value=new_active)

    save_btn.click(
        fn=_on_save,
        inputs=[ep_name, ep_provider, ep_base_url, ep_default_model,
                ep_api_key, ep_temperature, ep_set_active],
        outputs=[save_status, active_dd],
    )

    # Delete endpoint.
    def _on_delete(name):
        msg = _delete_endpoint(name)
        new_names = _load_endpoint_names()
        new_active = _load_active()
        # Load the new active endpoint's details.
        ep = _load_endpoint(new_active)
        return (
            msg,
            gr.update(choices=new_names, value=new_active),
            new_active,
            ep.get("provider", "ollama"),
            ep.get("base_url", ""),
            ep.get("default_model", ""),
            ep.get("api_key", ""),
            float(ep.get("temperature", 0.0)),
            False,
        )

    delete_btn.click(
        fn=_on_delete,
        inputs=[ep_name],
        outputs=[
            save_status, active_dd,
            ep_name, ep_provider, ep_base_url, ep_default_model,
            ep_api_key, ep_temperature, ep_set_active,
        ],
    )
