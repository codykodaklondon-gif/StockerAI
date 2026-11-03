"""Support handoff via user-reviewed email; no background submission."""
from urllib.parse import urlencode
import streamlit as st
import ui
from views.help_content import FAQS

EMAIL = "wilsonf20040708@gmail.com"
PHONE = "86301024"

def page_support():
    ui.heading("HELP & CONTACT", "Let’s get you back on track.", "Try these common fixes first. If you still need help, prepare a message and contact our team.")
    help_tab, contact_tab = st.tabs(["Common questions", "Message / human support"])
    with help_tab:
        for title, *steps in FAQS:
            with st.expander(title):
                for i, step in enumerate(steps, 1):
                    st.write(f"{i}. {step}")
        st.info("Still need help? Open Message / human support and tell us what happened.")
    with contact_tab:
        st.subheader("Tell us what happened")
        st.caption("Prepare an email, review it, then send it yourself. This is not live chat and does not automatically submit a ticket.")
        with st.form("support_message"):
            category = st.selectbox("Issue type", [row[0] for row in FAQS] + ["Other questions", "Partnership enquiry"])
            reply = st.text_input("Your email (optional)", max_chars=150)
            details = st.text_area("What happened?", placeholder="Which step failed? What message did you see? What would you like help with?", max_chars=3000)
            tried = st.text_area("What have you tried? (optional)", max_chars=1000)
            ready = st.form_submit_button("Prepare email", type="primary")
        if ready:
            if not details.strip():
                st.error("Please describe the issue first.")
                st.session_state.pop("support_draft", None)
            else:
                body = f"Issue type: {category}\nReply email: {reply.strip()}\n\nDescription: \n{details.strip()}\n\nSteps tried: \n{tried.strip()}"
                st.session_state.support_draft = (category, body)
        if draft := st.session_state.get("support_draft"):
            category, body = draft
            st.text_area("Email preview", body, disabled=True)
            url = "mailto:" + EMAIL + "?" + urlencode({"subject": f"[Stocker AI] {category}", "body": body})
            st.link_button("Open email to contact support", url, type="primary")
            st.download_button("Download message", body, "stocker-support.txt", "text/plain; charset=utf-8")
            st.caption("Your email has not been sent. If your email app does not open, download the message and send it to " + EMAIL + ". You can attach screenshots in your email.")
        st.divider()
        st.subheader("Contact us directly")
        st.markdown(f"**Support phone:** [{PHONE}](tel:{PHONE})  \n**Support email:** [{EMAIL}](mailto:{EMAIL})")
        st.link_button("Email about partnerships", "mailto:" + EMAIL + "?" + urlencode({"subject": "[Stocker AI] Partnership enquiry"}))
        st.caption("Never send passwords, API keys or full customer records. Hide personal information in error screenshots.")
