"""Voice questions: speech-to-text providers and the mic in the chat box.

Run from the project folder:  python -m unittest discover -s tests -v
No AI key or microphone is needed: the provider and the recording are replaced by fakes.
"""
import os
import sys
import types
import unittest
import uuid
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import openai  # noqa: E402
import streamlit as st  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

from stocker import agent, db, engine, sample  # noqa: E402

KEYS = [spec["env"] for spec in agent.PROVIDERS.values()] + ["STOCKER_MODEL"]


class FakeOpenAI:
    """Records the request and answers like the provider would."""
    calls = []

    def __init__(self, api_key=None, base_url=None):
        self.base_url = base_url
        self.audio = types.SimpleNamespace(transcriptions=types.SimpleNamespace(create=self._whisper))
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self._chat))

    def _whisper(self, **kw):
        FakeOpenAI.calls.append(("whisper", self.base_url, kw))
        return types.SimpleNamespace(text=" Hôm nay nên nhập bao nhiêu bánh mì? ")

    def _chat(self, **kw):
        FakeOpenAI.calls.append(("chat", self.base_url, kw))
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content='"How much bread?"'))])


class Transcribe(unittest.TestCase):
    def setUp(self):
        FakeOpenAI.calls = []
        self.env = mock.patch.dict(os.environ, {k: "" for k in KEYS})
        self.env.start()
        for k in KEYS:
            os.environ.pop(k, None)
        self.client = mock.patch.object(openai, "OpenAI", FakeOpenAI)
        self.client.start()

    def tearDown(self):
        agent.configure(None, None, None)
        self.client.stop()
        self.env.stop()

    def test_groq_uses_whisper_with_product_names(self):
        agent.configure("groq", "gsk_test", "llama-3.3-70b-versatile")
        self.assertEqual(agent.transcribe(b"RIFF", "White Bread, Teh Bags 100s"), "Hôm nay nên nhập bao nhiêu bánh mì?")
        kind, url, kw = FakeOpenAI.calls[0]
        self.assertEqual((kind, kw["model"]), ("whisper", agent.WHISPER))
        self.assertIn("Teh Bags", kw["prompt"])
        self.assertIn("groq", url)

    def test_gemini_listens_inside_a_chat_request(self):
        agent.configure("gemini", "AIza_test", "gemini-flash-latest")
        self.assertEqual(agent.transcribe(b"RIFF"), "How much bread?")
        kind, url, kw = FakeOpenAI.calls[0]
        parts = kw["messages"][0]["content"]
        self.assertEqual(kind, "chat")
        self.assertEqual(parts[1]["type"], "input_audio")
        self.assertEqual(parts[1]["input_audio"]["format"], "wav")

    def test_claude_falls_back_to_a_server_groq_key(self):
        agent.configure("claude", "sk-ant-test", "")
        os.environ["GROQ_API_KEY"] = "gsk_server"
        self.assertEqual(agent.speech_provider()[0], "groq")
        agent.transcribe(b"RIFF")
        self.assertEqual(FakeOpenAI.calls[0][0], "whisper")

    def test_without_a_speech_provider_the_owner_is_told_why(self):
        agent.configure("claude", "sk-ant-test", "")
        self.assertIsNone(agent.speech_provider())
        with self.assertRaises(agent.AIError):
            agent.transcribe(b"RIFF")


class FakeRecording:
    def getvalue(self):
        return b"RIFF....WAVE"


class VoiceInChat(unittest.TestCase):
    """The AI page with a key: the mic is offered, and a recording becomes the question."""

    def setUp(self):
        self.ws = "test" + uuid.uuid4().hex[:10]
        db.use_workspace(self.ws)
        sample.load_demo()
        engine.train_all()
        self.env = mock.patch.dict(os.environ, {"GROQ_API_KEY": "gsk_test", "STOCKER_MODEL": "llama-3.3-70b-versatile"})
        self.env.start()
        self.asked = []
        self.patches = [
            mock.patch.object(agent, "chat", lambda history, on_tool=None, **_: self.asked.append(history[-1]["content"]) or "Order 40 loaves."),
            mock.patch.object(agent, "transcribe", lambda audio, hint="": "How much bread should I order?"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.env.stop()
        db.use_workspace(self.ws)
        db.current().unlink(missing_ok=True)

    def app(self):
        os.chdir(ROOT)
        at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=180)
        at.query_params["ws"] = self.ws
        at.session_state["nav"] = "🤖 AI assistant"
        return at

    def test_mic_is_offered_and_typing_still_works(self):
        from views import assistant
        if not assistant.VOICE:
            self.skipTest("this Streamlit has no microphone in the chat box")
        at = self.app().run()
        self.assertFalse(at.exception, [e.value for e in at.exception])
        self.assertIn("mic", at.chat_input[0].proto.placeholder)
        at.chat_input[0].set_value("What should I order?").run()
        self.assertFalse(at.exception, [e.value for e in at.exception])
        self.assertEqual(self.asked, ["What should I order?"])

    def test_recording_becomes_the_question(self):
        from views import assistant
        if not assistant.VOICE:
            self.skipTest("this Streamlit has no microphone in the chat box")
        real = st.chat_input
        spoken = types.SimpleNamespace(text="", audio=FakeRecording())
        with mock.patch.object(st, "chat_input", lambda *a, **kw: spoken if kw.get("accept_audio") else real(*a, **kw)):
            at = self.app().run()
        self.assertFalse(at.exception, [e.value for e in at.exception])
        self.assertEqual(self.asked, ["How much bread should I order?"])
        self.assertTrue(any("How much bread" in m.markdown[0].value for m in at.chat_message if m.markdown))


class KeyChecks(unittest.TestCase):
    """A key typed in Settings: the sidebar status is right on every page, and a rejected key is explained."""

    def setUp(self):
        self.ws = "test" + uuid.uuid4().hex[:10]
        db.use_workspace(self.ws)
        sample.load_demo()
        engine.train_all()
        self.env = mock.patch.dict(os.environ, {k: "" for k in KEYS})
        self.env.start()
        for k in KEYS:
            os.environ.pop(k, None)

    def tearDown(self):
        agent.configure(None, None, None)
        self.env.stop()
        db.use_workspace(self.ws)
        db.current().unlink(missing_ok=True)

    def sidebar(self, key, models):
        os.chdir(ROOT)
        at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=180)
        at.query_params["ws"] = self.ws
        at.session_state["nav"] = "📥 Import data"
        at.session_state["llm_provider"] = "claude"
        at.session_state["llm_key"] = key
        with mock.patch.object(agent, "list_models", models):
            at.run()
        self.assertFalse(at.exception, [e.value for e in at.exception])
        return at, [c.value for c in at.sidebar.caption if c.value.startswith("AI:")]

    def test_status_shows_the_key_typed_in_settings(self):
        at, status = self.sidebar(f"  sk-ant-api03-{uuid.uuid4().hex[:16]}\n{uuid.uuid4().hex}\n", lambda p, k: ["claude-opus-5-5"])
        self.assertEqual(status, ["AI: 🟢 Anthropic Claude"])

    def test_a_rejected_key_is_explained_and_not_used(self):
        def reject(p, k):
            raise RuntimeError("Error code: 401 - invalid x-api-key")
        at, status = self.sidebar(f"sk-ant-usr-{uuid.uuid4().hex}", reject)
        self.assertEqual(status, ["AI: ⚪ off — add a key in Settings"])
        self.assertIn("rejected this key", at.sidebar.error[0].value)

    def test_errors_during_use_name_the_key_problem(self):
        agent.configure("groq", "gsk_old", "llama-3.3-70b-versatile")
        self.assertIn("rejected this key", agent.explain(RuntimeError("Error code: 401 - Invalid API Key")))
        self.assertIn("Settings → Model", agent.explain(RuntimeError("Error code: 404")))
        self.assertEqual(agent.explain(TimeoutError("slow")), "TimeoutError: slow")

    def test_busy_gemini_model_falls_back_to_lite(self):
        tried = []

        def create(model, **_):
            tried.append(model)
            if model == "gemini-flash-latest":
                raise RuntimeError("Error code: 429")
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content="ok"))])
        client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))
        self.assertEqual(agent._create(client, "gemini", "gemini-flash-latest", messages=[]).choices[0].message.content, "ok")
        self.assertEqual(tried, ["gemini-flash-latest", "gemini-flash-lite-latest"])
        with self.assertRaises(RuntimeError):            # other providers and other errors are not retried
            agent._create(client, "groq", "gemini-flash-latest", messages=[])

    def test_gemini_picks_the_current_flash_model(self):
        listed = ["models/gemini-2.5-flash", "models/gemini-2.5-flash-preview-tts", "models/aqa", "models/gemini-3.8-live",
                  "models/antigravity-preview-latest", "models/gemini-flash-lite-latest", "models/gemini-flash-latest"]
        fake = types.SimpleNamespace(models=types.SimpleNamespace(list=lambda: [types.SimpleNamespace(id=i) for i in listed]))
        with mock.patch.object(openai, "OpenAI", lambda **_: fake):
            models = agent.list_models("gemini", "AIza_test")
        self.assertEqual(models[0], "gemini-flash-latest")      # gemini-2.5-flash is still listed but answers 404
        self.assertFalse({"aqa", "gemini-3.8-live", "antigravity-preview-latest", "gemini-2.5-flash-preview-tts"} & set(models))


if __name__ == "__main__":
    unittest.main()
