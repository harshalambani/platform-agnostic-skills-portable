"""
ui/_theme.py — minimal black + electric blue Gradio theme.

Implements the palette locked in spec decision §15.4:

    Primary surface   #0A0A0A
    Secondary surface #171717
    Tertiary surface  #262626
    Body text         #F5F5F5
    Muted text        #A3A3A3
    Accent            #3B82F6
    Accent (hover)    #60A5FA
    Success           #10B981
    Warning           #F59E0B
    Error             #EF4444
"""
from __future__ import annotations

import gradio as gr


def make_theme() -> gr.themes.Base:
    """Return the project's custom Gradio theme."""
    theme = gr.themes.Base(
        primary_hue=gr.themes.Color(
            c50="#EFF6FF",
            c100="#DBEAFE",
            c200="#BFDBFE",
            c300="#93C5FD",
            c400="#60A5FA",
            c500="#3B82F6",
            c600="#2563EB",
            c700="#1D4ED8",
            c800="#1E40AF",
            c900="#1E3A8A",
            c950="#172554",
        ),
        secondary_hue="blue",
        neutral_hue=gr.themes.Color(
            c50="#FAFAFA",
            c100="#F5F5F5",
            c200="#E5E5E5",
            c300="#D4D4D4",
            c400="#A3A3A3",
            c500="#737373",
            c600="#525252",
            c700="#404040",
            c800="#262626",
            c900="#171717",
            c950="#0A0A0A",
        ),
        font=[gr.themes.GoogleFont("Inter"), "ui-sans-serif", "system-ui", "sans-serif"],
        font_mono=[gr.themes.GoogleFont("JetBrains Mono"), "ui-monospace", "monospace"],
    ).set(
        body_background_fill="#0A0A0A",
        body_text_color="#F5F5F5",
        background_fill_primary="#0A0A0A",
        background_fill_secondary="#171717",
        block_background_fill="#171717",
        block_border_color="#262626",
        block_label_text_color="#A3A3A3",
        input_background_fill="#262626",
        input_border_color="#404040",
        button_primary_background_fill="#3B82F6",
        button_primary_background_fill_hover="#60A5FA",
        button_primary_text_color="#F5F5F5",
        button_secondary_background_fill="#262626",
        button_secondary_background_fill_hover="#404040",
        button_secondary_text_color="#F5F5F5",
        color_accent="#3B82F6",
        color_accent_soft="#1E3A8A",
        # gr.Dataframe (e.g. the History tab table) otherwise keeps Gradio's
        # light-mode row defaults (table_even_background_fill="white",
        # table_odd_background_fill="*neutral_50") — near-invisible against
        # body_text_color forced to near-white above. Same class of bug
        # already patched for gr.File's preview table in webui.py's APP_CSS.
        table_even_background_fill="#171717",
        table_odd_background_fill="#0A0A0A",
        table_border_color="#262626",
        table_text_color="#F5F5F5",
    )
    return theme
