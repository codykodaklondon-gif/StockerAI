"""Pricing: a demo list of plans. Nothing here charges money or changes what the app allows."""
import streamlit as st
import ui

PLANS = [  # (name, price, who it is for, what is included, what is not)
    ("Free", "S$0", "Try Stocker AI in your shop.",
     ["Sales import from a file", "Dashboard, forecast and order planning"],
     ["Only 5 AI questions a day", "No import from a photo", "No holiday promotions"]),
    ("Personal", "S$18", "For one shop owner.",
     ["Everything in Free", "Unlimited AI questions", "Import from a photo", "Holiday promotions"], []),
    ("Business", "S$30", "For a shop with staff.",
     ["Everything in Personal", "Several shops and staff", "Priority support"], []),
]


def page_pricing():
    ui.heading("PRICING", "Choose a plan.", "Prices are in Singapore dollars, per month.")
    st.caption("Demo only: no payment is taken, and the app works the same on every plan for now.")
    for col, (name, price, who, included, missing) in zip(st.columns(len(PLANS)), PLANS):
        with col.container(border=True):
            st.subheader(name)
            st.markdown(f"## {price}")
            st.caption(f"per month · {who}")
            for item in included:
                st.markdown(f":material/check: {item}")
            for item in missing:
                st.markdown(f":material/close: :gray[{item}]")
