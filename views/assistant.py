"""Ask Stocker AI: the chat shared by the Welcome and AI pages, and download buttons for AI-made files."""
import inspect

import streamlit as st
from stocker import agent, db, exports
from views.common import AI_LIMIT, cached_briefing, data_key, has_data


def chat_panel(where: str, height: int | None = None):
    """The chat itself, shared by the Welcome page and the AI assistant page (one conversation for both).
    With a height the messages scroll inside a box and the input sits under it, instead of at the page bottom.
    Files the AI creates (CSV / Excel / Word) appear as download buttons under its reply."""
    if not has_data():
        st.info("Import your sales or load the demo shop first, then ask about it here.")
        return
    pending = st.session_state.pop("pending_question", None) if where == "ai" else None
    if not agent.available():
        if pending:
            st.chat_message("user").markdown(pending)
        st.info("Add a free Groq or Gemini API key under **Settings** in the sidebar to enable chat. "
                "Here's today's briefing instead:")
        if height:
            st.container(height=height).markdown(cached_briefing(data_key()))
        else:
            st.markdown(cached_briefing(data_key()))
        return
    st.session_state.setdefault("history", [])     # API messages
    st.session_state.setdefault("display", [])     # (role, text, files)
    box = st.container(height=height, border=False) if height else st.container()
    with box:
        for i, (role, text, *files) in enumerate(st.session_state.display):
            with st.chat_message(role):
                st.markdown(text)
                file_buttons(files[0] if files else [], key=f"{where}_hist{i}")
        if not st.session_state.display:
            st.caption("Try: *What should I order today?* · *Which items are at risk?* · "
                       "*Export this week's shopping list to Excel* · *Make a Word report of my risks*")
    own_key = agent.active() != agent.shared()
    used = st.session_state.get("ai_used", 0)
    if not own_key and used >= AI_LIMIT:
        st.warning(f"You've used all {AI_LIMIT} free questions for this session. "
                   "Add your own free Groq key under **Settings** in the sidebar to keep chatting.")
        return
    voice = VOICE and agent.speech_provider() is not None
    value = st.chat_input("Ask Stocker AI… or tap the mic to speak" if voice else "Ask Stocker AI…",
                          key=f"chat_input_{where}", **({"accept_audio": True} if voice else {}))
    typed = getattr(value, "text", value) or None
    heard = None
    if voice and not typed and (audio := getattr(value, "audio", None)):
        with box, st.spinner("Listening…"):
            try:
                typed = heard = agent.transcribe(audio.getvalue(), speech_hint())
            except Exception as e:   # bad key, rate limit, network: show it, keep the app alive
                st.error(f"Could not turn the recording into text. {agent.explain(e)}")
                return
    if q := typed or pending:   # also questions asked from My shop
        st.session_state.ai_used = used + (0 if own_key else 1)
        shown = f":material/mic: {q}" if heard else q
        with box:
            st.chat_message("user").markdown(shown)
            st.session_state.display.append(("user", shown))
            st.session_state.history.append({"role": "user", "content": q})
            with st.chat_message("assistant"):
                status = st.status("Analysing…")
                live, written = st.empty(), []   # the answer as it is written, before it is complete

                def on_text(chunk):
                    written.append(chunk)
                    live.markdown("".join(written))

                def on_tool(name, args):
                    written.clear()          # text before a tool call is only a lead-in to it
                    live.empty()
                    status.write(f"🔧 {name} {args or ''}")
                try:
                    agent.start_turn()
                    answer = agent.chat(st.session_state.history, on_tool=on_tool, on_text=on_text)
                    status.update(label="Done", state="complete")
                except Exception as e:   # bad key, rate limit, network: show it, keep the app alive
                    st.session_state.history.pop()
                    st.session_state.display.pop()
                    status.update(label="AI request failed", state="error")
                    live.empty()
                    st.error(agent.explain(e))
                    return
                live.markdown(answer)
                files = agent.created_files()
                file_buttons(files, key=f"{where}_new{len(st.session_state.display)}")
        st.session_state.display.append(("assistant", answer, files))


# the mic inside the chat box needs Streamlit 1.5x+; older installs simply keep typing only
VOICE = "accept_audio" in inspect.signature(st.chat_input).parameters


def speech_hint() -> str:
    """Product names, so speech-to-text spells them the shop's way."""
    return ", ".join(db.products()["name"].head(60))


def page_ai():
    st.header("Ask Stocker AI")
    st.caption("Ask about your shop in plain language, by typing or by voice. Numbers come from your own sales "
               "records; the AI explains them.")
    if VOICE and agent.available() and agent.speech_provider() is None:
        st.caption(":material/mic_off: Voice questions need a free Groq or Gemini key under Settings "
                   "(Claude cannot listen to audio).")
    chat_panel("ai")


FILE_ICONS = {"csv": ":material/csv:", "xlsx": ":material/table_view:", "docx": ":material/description:"}


def file_buttons(files: list[dict], key: str):
    """Download buttons for files the AI created; contents are built by the app from real data."""
    for j, info in enumerate(files):
        f = exports.get(info["file_id"])
        if f is None:
            st.caption(f":material/history: {info['filename']} has expired — ask again to recreate it.")
            continue
        st.download_button(f"Download {f['name']}", f["data"], f["name"], f["mime"],
                           icon=FILE_ICONS.get(f["format"]), key=f"dl_{key}_{j}")
