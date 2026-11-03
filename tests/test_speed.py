"""Faster AI answers: thinking level per task, streamed Claude answers, shorter old tool results.

Run from the project folder:  python -m unittest discover -s tests -v
No AI key is needed: the providers are replaced by fakes.
"""
import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import anthropic  # noqa: E402
import openai  # noqa: E402

from stocker import agent  # noqa: E402

KEYS = [s["env"] for s in agent.PROVIDERS.values()] + ["STOCKER_MODEL"]


def reply(content="", calls=None):
    msg = types.SimpleNamespace(content=content, tool_calls=calls)
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])


class FakeOpenAI:
    """Records each chat request; replies with the queued answers."""
    requests, replies = [], []

    def __init__(self, api_key=None, base_url=None):
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self._create))

    def _create(self, **kw):
        FakeOpenAI.requests.append(kw)
        if kw.get("reasoning_effort") and "llama" in kw["model"]:
            raise RuntimeError("Error code: 400 - reasoning_effort is not supported with this model")
        return FakeOpenAI.replies.pop(0)


class Base(unittest.TestCase):
    def setUp(self):
        FakeOpenAI.requests, FakeOpenAI.replies = [], []
        agent._no_effort.clear()
        self.patches = [mock.patch.dict(os.environ, {k: "" for k in KEYS}),
                        mock.patch.object(openai, "OpenAI", FakeOpenAI)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        agent.configure()


class ThinkingLevel(Base):
    def test_level_per_task_and_only_for_thinking_models(self):
        self.assertEqual(agent._effort("gemini", "gemini-flash-latest", "chat"), {"reasoning_effort": "low"})
        self.assertEqual(agent._effort("gemini", "gemini-flash-latest", "plan"), {"reasoning_effort": "medium"})
        self.assertEqual(agent._effort("groq", "openai/gpt-oss-120b", "chat"), {"reasoning_effort": "low"})
        self.assertEqual(agent._effort("groq", "llama-3.3-70b-versatile", "chat"), {})
        self.assertEqual(agent._effort("gemini", "gemini-flash-latest", None), {})

    def test_chat_asks_gemini_to_think_briefly(self):
        agent.configure("gemini", "AIza_test", "gemini-flash-latest")
        FakeOpenAI.replies = [reply("Order 40 loaves.")]
        self.assertEqual(agent.chat([{"role": "user", "content": "Bread?"}]), "Order 40 loaves.")
        sent = FakeOpenAI.requests[0]
        self.assertEqual(sent["reasoning_effort"], "low")
        self.assertIn("call them all in the same turn", sent["messages"][0]["content"])

    def test_plan_keeps_more_thinking(self):
        agent.configure("gemini", "AIza_test", "gemini-flash-latest")
        FakeOpenAI.replies = [reply('{"plan": []}')]
        self.assertEqual(agent.complete_json("system", "user"), {"plan": []})
        self.assertEqual(FakeOpenAI.requests[0]["reasoning_effort"], "medium")

    def test_a_model_that_refuses_the_setting_is_asked_again_without_it(self):
        agent._no_effort.clear()
        with mock.patch.object(agent, "THINKING_MODELS", ("gemini", "gpt-oss", "llama")):
            agent.configure("groq", "gsk_test", "llama-3.3-70b-versatile")
            FakeOpenAI.replies = [reply("one"), reply("two")]
            self.assertEqual(agent.chat([{"role": "user", "content": "Hi"}]), "one")
            self.assertEqual([("reasoning_effort" in r) for r in FakeOpenAI.requests], [True, False])
            self.assertEqual(agent.chat([{"role": "user", "content": "Hi"}]), "two")   # remembered: one request
            self.assertEqual(len(FakeOpenAI.requests), 3)
            self.assertNotIn("reasoning_effort", FakeOpenAI.requests[2])


class ShorterHistory(unittest.TestCase):
    def test_old_tool_results_are_shortened_in_the_request_only(self):
        full = json.dumps({"rows": list(range(500))})
        history = []
        for q in ("first", "second", "third"):
            history += [{"role": "user", "content": q},
                        {"role": "assistant", "content": "", "tool_calls": [{"id": q}]},
                        {"role": "tool", "tool_call_id": q, "content": full},
                        {"role": "assistant", "content": "answer"}]
        sent = agent._trimmed(history)
        self.assertEqual([m["content"] == full for m in sent if m["role"] == "tool"], [False, True, True])
        self.assertTrue(all(m["content"] == full for m in history if m["role"] == "tool"))   # history unchanged
        short = history[:8]                                                                  # two questions: nothing to cut
        self.assertIs(agent._trimmed(short), short)

    def test_claude_tool_results_are_shortened_too(self):
        full = "x" * 5000
        history = []
        for q in ("first", "second", "third"):
            history += [{"role": "user", "content": q},
                        {"role": "assistant", "content": [types.SimpleNamespace(type="tool_use")]},
                        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": q, "content": full}]},
                        {"role": "assistant", "content": [types.SimpleNamespace(type="text")]}]
        sent = agent._trimmed(history)
        results = [m["content"][0]["content"] for m in sent if m["role"] == "user" and isinstance(m["content"], list)]
        self.assertEqual([r == full for r in results], [False, True, True])
        self.assertIn("Call the tool again", results[0])


class FakeStream:
    """Stands in for client.beta.messages.stream(): text chunks, then the final message."""
    def __init__(self, chunks, final):
        self.text_stream, self.final = iter(chunks), final

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self.final


class StreamedClaude(Base):
    def test_answer_arrives_in_pieces(self):
        tool = types.SimpleNamespace(type="tool_use", id="t1", name="find_product", input={"query": "bread"})
        steps = [FakeStream([], types.SimpleNamespace(stop_reason="tool_use", content=[tool])),
                 FakeStream(["Order ", "40 ", "loaves."], types.SimpleNamespace(
                     stop_reason="end_turn", content=[types.SimpleNamespace(type="text", text="Order 40 loaves.")]))]
        sent = []

        def stream(**kw):
            sent.append(kw)
            return steps.pop(0)
        fake = types.SimpleNamespace(beta=types.SimpleNamespace(messages=types.SimpleNamespace(stream=stream)))
        agent.configure("claude", "sk-ant-api03-test", "claude-opus-5-5")
        chunks, tools = [], []
        with mock.patch.object(anthropic, "Anthropic", lambda **_: fake), \
                mock.patch.object(agent, "run_tool", lambda n, a: {"sku": "BRD-01"}):
            answer = agent.chat([{"role": "user", "content": "Bread?"}], on_tool=lambda n, a: tools.append(n),
                                on_text=chunks.append)
        self.assertEqual(answer, "Order 40 loaves.")
        self.assertEqual(chunks, ["Order ", "40 ", "loaves."])
        self.assertEqual(tools, ["find_product"])
        self.assertEqual(sent[0]["output_config"], {"effort": "low"})


class ChatPage(unittest.TestCase):
    def test_streamed_text_is_replaced_by_the_full_answer(self):
        from streamlit.testing.v1 import AppTest

        def chat(history, on_tool=None, on_text=None, **_):
            on_text("Order ")
            on_tool("find_product", {"query": "bread"})
            on_text("Order 40 ")
            on_text("loaves.")
            history.append({"role": "assistant", "content": "Order 40 loaves."})
            return "Order 40 loaves."
        with mock.patch.dict(os.environ, {"GROQ_API_KEY": "gsk_test", "STOCKER_MODEL": "llama-3.3-70b-versatile"}), \
                mock.patch.object(agent, "chat", chat):
            os.chdir(ROOT)
            at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=120)
            at.session_state["nav"] = "🤖 AI assistant"
            at.run()
            at.chat_input[0].set_value("How much bread?").run()
        self.assertFalse(at.exception, [e.value for e in at.exception])
        texts = [m.value for m in at.markdown]
        self.assertIn("Order 40 loaves.", texts)
        self.assertNotIn("Order ", texts)


if __name__ == "__main__":
    unittest.main()
