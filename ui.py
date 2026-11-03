"""Presentation helpers for the neighbourhood theme; no business calculations."""
from html import escape
from pathlib import Path

import streamlit as st

from stocker.icons import icon

ROOT = Path(__file__).resolve().parent


def theme():
    st.html(f"<style>{(ROOT / 'assets' / 'neighbourhood.css').read_text()}</style>")


def go_to(label):
    st.session_state.nav = label


def heading(eyebrow, title, description):
    st.html(f'<div class="page-heading"><div class="eyebrow">{escape(eyebrow)}</div>'
            f'<h1>{escape(title)}</h1><p>{escape(description)}</p></div>')


def badge(label, tone="sage"):
    tones = {"coral", "sage", "amber", "red", "neutral"}
    tone = tone if tone in tones else "neutral"
    st.html(f'<span class="badge {tone}">{escape(label)}</span>')


def product_heading(name, category="", symbol=None):
    """Product name and a subtitle, with the product's icon (from its name or category) in the square."""
    symbol = symbol or icon(name, category)
    st.html(f'<div class="product-heading"><span class="product-symbol" aria-hidden="true">{escape(symbol)}</span>'
            f'<div><h3>{escape(str(name))}</h3><span>{escape(str(category))}</span></div></div>')


def chart_style(fig):
    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="Arial, sans-serif", color="#493A31", size=12),
        margin=dict(l=8, r=8, t=16, b=8),
        legend=dict(orientation="h", y=-0.18, x=0),
    )
    fig.update_xaxes(showgrid=False, zeroline=False)
    fig.update_yaxes(gridcolor="#EDE7DC", zeroline=False)
    return fig
