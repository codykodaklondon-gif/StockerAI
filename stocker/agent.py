"""AI assistant layer.

The model never sees raw data and never computes numbers. It can only call the
analysis tools below, which return results from the deterministic engine, and then
explain them. If no API key is configured, the rest of the app keeps working.
"""
import json
import os
from contextvars import ContextVar
from functools import lru_cache

import pandas as pd

from . import advisor, db, engine, events, exports

# Provider-agnostic: any of these works. Free tiers (Groq, Gemini, OpenRouter) are
# plenty for a demo. Numbers never depend on the provider, only the wording does.
PROVIDERS = {
    "groq":       {"label": "Groq (free)", "env": "GROQ_API_KEY",
                   "base_url": "https://api.groq.com/openai/v1", "model": "llama-3.3-70b-versatile"},
    "gemini":     {"label": "Google Gemini (free)", "env": "GEMINI_API_KEY",
                   "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/", "model": "gemini-flash-latest"},
    "openrouter": {"label": "OpenRouter", "env": "OPENROUTER_API_KEY",
                   "base_url": "https://openrouter.ai/api/v1", "model": "meta-llama/llama-3.3-70b-instruct:free"},
    "claude":     {"label": "Anthropic Claude", "env": "ANTHROPIC_API_KEY", "model": "claude-opus-5-5"},
}

# Runtime settings chosen in the UI. A ContextVar keeps each visitor's key private to their
# own session; without one, the shared key from the environment / Streamlit secrets is used.
_config: ContextVar[dict] = ContextVar("stocker_llm", default={})


def configure(provider: str | None = None, key: str | None = None, model: str | None = None):
    _config.set({"provider": provider, "key": "".join((key or "").split()) or None, "model": model or None})


def shared() -> tuple[str, str, str] | None:
    """The host's shared key, if one is configured on the server."""
    for p, spec in PROVIDERS.items():
        if key := os.environ.get(spec["env"]):
            return p, key, os.environ.get("STOCKER_MODEL") or _auto_model(p, key)
    return None


@lru_cache(maxsize=8)
def _auto_model(provider: str, key: str) -> str:
    """Best available model for the shared key; falls back to the built-in default."""
    try:
        return (list_models(provider, key) or [PROVIDERS[provider]["model"]])[0]
    except Exception:
        return PROVIDERS[provider]["model"]


def active() -> tuple[str, str, str] | None:
    """Return (provider, api_key, model): the visitor's own key first, then the shared one."""
    cfg = _config.get()
    if cfg.get("provider") and cfg.get("key"):
        p = cfg["provider"]
        return p, cfg["key"], cfg["model"] or PROVIDERS[p]["model"]
    return shared()

# Preferred models in order; providers retire models often, so the app picks the
# first one the key actually has access to instead of relying on a fixed name.
# Gemini still lists retired models (gemini-2.5-flash answers 404 for newer keys), so its
# "-latest" alias, which Google keeps pointing at a current model, comes first.
PREFERRED = ["gpt-oss-120b", "llama-3.3-70b", "llama-4-maverick", "kimi-k2", "qwen3-32b",
             "llama-4-scout", "gemini-flash-latest", "gemini-flash", "gpt-oss-20b", "llama"]
SKIP = ("whisper", "tts", "guard", "embed", "playai", "orpheus", "compound", "vision", "image", "audio",
        "live", "lyria", "robotics", "computer-use", "deep-research", "antigravity", "aqa", "transcribe",
        "banana", "translate", "customtools")


def list_models(provider: str, key: str) -> list[str]:
    """Chat models this key can use, best candidates first."""
    if provider == "claude":
        import anthropic
        anthropic.Anthropic(api_key=key).models.list(limit=1)   # raises if Anthropic rejects the key
        return [PROVIDERS["claude"]["model"]]
    from openai import OpenAI

    ids = [m.id for m in OpenAI(api_key=key, base_url=PROVIDERS[provider]["base_url"]).models.list()]
    ids = [i.removeprefix("models/") for i in ids if not any(s in i.lower() for s in SKIP)]

    def rank(i):
        return next((n for n, p in enumerate(PREFERRED) if p in i.lower()), len(PREFERRED))
    return sorted(ids, key=lambda i: (rank(i), i))


KEY_PAGES = {"groq": "console.groq.com/keys", "gemini": "aistudio.google.com/apikey",
             "openrouter": "openrouter.ai/keys", "claude": "console.anthropic.com → API keys"}


def key_problem(provider: str, key: str, error: Exception) -> str:
    """Plain message for a key the provider would not accept."""
    text = str(error)
    if "401" in text or "403" in text or "invalid" in text.lower() or "authentication" in text.lower():
        return (f"{PROVIDERS[provider]['label']} rejected this key. Check it was copied in full, from the first "
                f"character to the last, or create a new one at {KEY_PAGES[provider]}.")
    return f"Could not verify key: {text[:200]}"


def explain(error: Exception) -> str:
    """Error text for the owner; a rejected key gets advice instead of the raw provider reply."""
    text = str(error)
    act = active()
    if act and ("401" in text or "authentication" in text.lower() or "api key not valid" in text.lower()):
        return key_problem(act[0], act[1], error)
    if act and ("404" in text or "not found" in text.lower()):
        return (f"The model “{act[2]}” is not available to this {PROVIDERS[act[0]]['label']} key. "
                f"Choose another one under Settings → Model (for Gemini: {PROVIDERS['gemini']['model']}).")
    return f"{type(error).__name__}: {text}"


SYSTEM = """You are Stocker AI, an inventory assistant for a small business owner.

Rules:
- Every number you state must come from a tool result in this conversation. Never estimate, \
calculate or invent figures yourself. If a tool cannot answer, say so.
- Recommendations are for reference only; the owner makes the final decision and you never place orders.
- Answer in the same language the user writes in. Be brief and practical: lead with the answer, \
then the evidence (e.g. "Saturdays run 1.3x weekdays"). Use short bullet lists for multiple items.
- When asked about a product by name, call find_product first if you are unsure of its SKU.
- When you need several tools, call them all in the same turn instead of one after another.
- When the owner asks for a file (CSV, Excel, Word, report, spreadsheet, download), call export_file with the
  matching tables; for a forecast table use the exact sku. Then say briefly what is in the file — the app shows
  a download button under your reply. Never paste the file's contents."""


def _records(df: pd.DataFrame, cols=None, limit=50):
    if df.empty:
        return []
    if cols:
        df = df[[c for c in cols if c in df.columns]]
    return json.loads(df.head(limit).to_json(orient="records", date_format="iso"))


def t_overview(**_):
    return {"kpis": advisor.kpis(), "model": engine.training_summary(),
            "risk_counts": advisor.risks_frame().get("severity", pd.Series(dtype=str)).value_counts().to_dict()}


def t_find_product(query: str, **_):
    p = db.products()
    m = p[p["name"].str.contains(query, case=False, na=False) | p["sku"].str.contains(query, case=False, na=False)]
    return _records(m, ["sku", "name", "category", "current_stock", "unit_price", "lead_time_days"], 10)


def t_recommendations(action: str | None = None, **_):
    r = advisor.recommendations()
    if action and not r.empty:
        r = r[r["action"].str.lower() == action.lower()]
    return _records(r)


def t_risks(min_severity: str = "low", **_):
    rank = advisor.SEVERITY_ORDER
    r = advisor.risks_frame()
    if r.empty:
        return []
    return _records(r[r["severity"].map(rank) <= rank.get(min_severity, 3)])


def t_forecast(sku: str, days: int = 7, **_):
    m = engine.load_models().get(sku)
    if not m:
        return {"error": f"No trained model for {sku}."}
    fc = m.forecast(min(int(days), 30), start=advisor.plan_start())
    fc["weekday"] = fc["date"].dt.day_name()
    return {"sku": sku, "status": m.status, "forecast": _records(fc.round({"forecast": 1, "low": 1, "high": 1})),
            "total": round(float(fc["forecast"].sum()), 1), "evidence": engine.explain(m)}


def t_product_analysis(sku: str, **_):
    m = engine.load_models().get(sku)
    if not m:
        return {"error": f"No trained model for {sku}."}
    s = engine.daily_series(sku)
    weekly = s.resample("W").sum().tail(8)
    return {
        "sku": sku, "days_of_history": m.n_days, "avg_last_7": round(m.avg_7, 1),
        "avg_last_28": round(m.avg_28, 1),
        "weekday_multipliers": dict(zip(engine.DOW_NAMES, [round(x, 2) for x in m.dow])),
        "last_8_weeks": {d.date().isoformat(): float(v) for d, v in weekly.items()},
        "backtest_error": m.wape, "naive_error": m.naive_wape, "evidence": engine.explain(m),
        "model": "machine learning" if m.method == "ml" else "standard",
        "method": "machine learning" if m.method == "ml" else engine.VARIANTS.get(m.variant, m.variant),
        "weekly_total_error": m.week_wape, "naive_weekly_total_error": m.naive_week_wape,
        "weeks_above_80pct_range": m.weeks_over,
    }


def t_top_sellers(days: int = 30, **_):
    s = db.sales()
    if s.empty:
        return []
    s = s[s["date"] > s["date"].max() - pd.Timedelta(days=int(days))]
    g = s.groupby("sku").agg(units=("qty", "sum"), revenue=("revenue", "sum")).reset_index()
    g = g.merge(db.products()[["sku", "name"]], on="sku").sort_values("revenue", ascending=False)
    return _records(g.round(2), limit=20)


def t_customer_profile(**_):
    from . import profile
    p = profile.build()
    if not p.get("available"):
        return {"error": "No sales data."}
    out = {"capabilities": p["capabilities"], "insights": p["insights"],
           "weekday_weekend": _records(p["weekday_weekend"].reset_index())}
    if "slot_by_category" in p:
        out["slot_share_by_category"] = _records(p["slot_by_category"].reset_index())
    if "customer_types" in p:
        out["customer_types"] = _records(p["customer_types"].reset_index())
    if "baskets" in p:
        out["baskets"] = p["baskets"]
    if "segments" in p:
        out["segments"] = _records(p["segments"].reset_index())
        out["repeat_rate_30d"] = p["repeat_rate"]
    return out


# files created during the current chat turn (read by the app to show download buttons)
_created: ContextVar[list] = ContextVar("stocker_created_files", default=[])


def start_turn():
    _created.set([])


def created_files() -> list[dict]:
    return list(_created.get())


def t_export_file(format: str = "xlsx", title: str = "", sections: list | None = None, **_):
    info = exports.build(format, title, sections or [])
    _created.set(_created.get() + [info])
    return {**info, "note": "The download button is shown below your reply. Do not paste the file contents."}


def t_events(days: int = 30, **_):
    return _records(events.upcoming(min(int(days), 365)), limit=40)


TOOLS = {
    "get_overview": (t_overview, "Shop-wide KPIs, model accuracy and risk counts.", {}),
    "find_product": (t_find_product, "Look up products by (partial) name or SKU.",
                     {"query": {"type": "string"}}),
    "get_recommendations": (t_recommendations,
                            "Today's stocking list with quantity ranges and reasons. Optionally filter by action: "
                            "'Stock more', 'Stock less', 'Maintain', 'Stop reordering'.",
                            {"action": {"type": "string"}}),
    "get_risks": (t_risks, "Current inventory risks (stock-out, expiry, overstock, dead stock, demand anomalies).",
                  {"min_severity": {"type": "string", "enum": ["critical", "high", "medium", "low"]}}),
    "get_forecast": (t_forecast, "Daily demand forecast with ranges for one product.",
                     {"sku": {"type": "string"}, "days": {"type": "integer"}}),
    "get_product_analysis": (t_product_analysis, "Sales history, weekday pattern and trend for one product.",
                             {"sku": {"type": "string"}}),
    "get_customer_profile": (t_customer_profile,
                             "Anonymous customer profile: busy hours, time-of-day mix by category, weekday vs weekend, "
                             "member vs walk-in, basket size, items bought together, customer segments, repeat rate.", {}),
    "get_top_sellers": (t_top_sellers, "Best-selling products by revenue over the last N days.",
                        {"days": {"type": "integer"}}),
    "get_upcoming_events": (t_events, "Holidays, promotions and closures in the next N days from the shop's calendar "
                                      "(forecasts already include their effect; uplift is the owner's expected % change).",
                            {"days": {"type": "integer"}}),
}
TOOLS["export_file"] = (
    t_export_file,
    "Create a downloadable file (CSV, Excel or Word) for the owner. Tables are filled by the app from the shop's "
    "real data; for Word/Excel you may add your own short explanatory text per section. Available tables: "
    + "; ".join(f"{k} = {v[1]}" for k, v in exports.DATASETS.items()),
    {"format": {"type": "string", "enum": ["csv", "xlsx", "docx"],
                "description": "csv, xlsx (Excel) or docx (Word)"},
     "title": {"type": "string"},
     "sections": {"type": "array", "items": {"type": "object", "properties": {
         "heading": {"type": "string"},
         "table": {"type": "string", "enum": list(exports.DATASETS)},
         "params": {"type": "object", "properties": {"sku": {"type": "string"}, "days": {"type": "integer"}}},
         "text": {"type": "string", "description": "Your explanation for this section (Word/Excel only)"}}}}},
)
REQUIRED = {"find_product": ["query"], "get_forecast": ["sku"], "get_product_analysis": ["sku"],
            "export_file": ["format", "title", "sections"]}

TOOL_DEFS = [
    {"name": n, "description": d,
     "input_schema": {"type": "object", "properties": props, "required": REQUIRED.get(n, [])}}
    for n, (_, d, props) in TOOLS.items()
]


def available() -> bool:
    return active() is not None


def run_tool(name: str, args: dict):
    fn = TOOLS.get(name, (None,))[0]
    if fn is None:
        return {"error": f"Unknown tool {name}"}
    try:
        return fn(**(args or {}))
    except Exception as e:  # tool errors go back to the model, not the user
        return {"error": str(e)}


def chat(messages: list, on_tool=None, max_steps: int = 8, on_text=None) -> str:
    """Run the tool-use loop with the active provider. `messages` is mutated in place.
    `on_text(chunk)` receives the answer while it is being written (Claude only; others arrive at once)."""
    provider, key, model = active()
    if provider == "claude":
        return _chat_claude(messages, key, model, on_tool, max_steps, on_text)
    return _chat_openai_compatible(messages, provider, key, model, on_tool, max_steps)


OPENAI_TOOLS = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                   "parameters": t["input_schema"]}} for t in TOOL_DEFS]


# A lighter model to move to when the chosen one is busy (503), out of free quota (429) or retired (404).
# Gemini's free tier often limits its newest Flash model while Flash-Lite still answers.
FALLBACK = {"gemini": ["gemini-flash-lite-latest"]}
BUSY = ("Error code: 429", "Error code: 503", "Error code: 404")


# How long models that think before answering (Gemini, gpt-oss on Groq) may think. Gemini Flash thinks at
# "high" by default, which is most of the waiting. Chat numbers come from the tools, so "low" is enough;
# a purchase plan has to fit a budget, so it keeps a little more.
EFFORT = {"chat": "low", "plan": "medium", "vision": "low", "speech": "low"}
THINKING_MODELS = ("gemini", "gpt-oss")
_no_effort: set = set()   # (provider, model) pairs that rejected reasoning_effort


def _effort(provider, model, task) -> dict:
    if not task or (provider, model) in _no_effort or not any(n in model.lower() for n in THINKING_MODELS):
        return {}
    return {"reasoning_effort": EFFORT[task]}


def _create(client, provider, model, task=None, **kw):
    """client.chat.completions.create, retried once on the provider's fallback model.
    `task` (a key of EFFORT) sets how long a thinking model may think."""
    models = [model] + [m for m in FALLBACK.get(provider, []) if m != model]
    for i, m in enumerate(models):
        try:
            try:
                return client.chat.completions.create(model=m, **kw, **_effort(provider, m, task))
            except Exception as e:   # a model without a thinking setting: ask again without it
                if "reasoning" not in str(e).lower() or not _effort(provider, m, task):
                    raise
                _no_effort.add((provider, m))
                return client.chat.completions.create(model=m, **kw)
        except Exception as e:
            if i == len(models) - 1 or not any(b in str(e) for b in BUSY):
                raise


# Tool results from older questions are shortened before each request, so a long chat does not get slower
# and slower. The model calls the tool again if it needs those numbers.
KEEP_TOOL_RESULTS = 2   # questions whose tool results are sent in full (the current one and the one before)
OLD_RESULT = json.dumps({"note": "Result from an earlier question, removed to save time. Call the tool again if needed."})


def _trimmed(messages: list) -> list:
    """Copy of the history with tool results before the last KEEP_TOOL_RESULTS questions shortened."""
    questions = [i for i, m in enumerate(messages) if m["role"] == "user" and isinstance(m["content"], str)]
    if len(questions) <= KEEP_TOOL_RESULTS:
        return messages
    cut, out = questions[-KEEP_TOOL_RESULTS], []
    for i, m in enumerate(messages):
        if i < cut and m["role"] == "tool":
            m = {**m, "content": OLD_RESULT}
        elif i < cut and m["role"] == "user" and isinstance(m["content"], list):
            m = {**m, "content": [{**b, "content": OLD_RESULT} if isinstance(b, dict) and b.get("type") == "tool_result"
                                  else b for b in m["content"]]}
        out.append(m)
    return out


def _chat_openai_compatible(messages, provider, key, model, on_tool, max_steps):
    from openai import OpenAI

    client = OpenAI(api_key=key, base_url=PROVIDERS[provider]["base_url"])
    for _ in range(max_steps):
        resp = _create(
            client, provider,
            model=model,
            task="chat",
            messages=[{"role": "system", "content": SYSTEM}] + _trimmed(messages),
            tools=OPENAI_TOOLS,
            tool_choice="auto",
            temperature=0.2,
        )
        msg = resp.choices[0].message
        calls = msg.tool_calls or []
        messages.append({"role": "assistant", "content": msg.content or "",
                         **({"tool_calls": [c.model_dump() for c in calls]} if calls else {})})
        if not calls:
            return msg.content or ""
        for c in calls:
            try:
                args = json.loads(c.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            if on_tool:
                on_tool(c.function.name, args)
            messages.append({"role": "tool", "tool_call_id": c.id,
                             "content": json.dumps(run_tool(c.function.name, args), default=str)})
    return "I needed too many steps to answer that. Please try a more specific question."


def _chat_claude(messages, key, model, on_tool, max_steps, on_text=None):
    import anthropic

    client = anthropic.Anthropic(api_key=key)
    for _ in range(max_steps):
        with client.beta.messages.stream(       # streamed, so the answer shows while it is written
            model=model,
            max_tokens=16000,
            system=SYSTEM,
            tools=TOOL_DEFS,
            messages=_trimmed(messages),
            output_config={"effort": "low"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        ) as stream:
            if on_text:
                for chunk in stream.text_stream:
                    on_text(chunk)
            resp = stream.get_final_message()
        if resp.stop_reason == "refusal":
            return "Sorry, I can't help with that request."
        messages.append({"role": "assistant", "content": resp.content})
        calls = [b for b in resp.content if b.type == "tool_use"]
        if resp.stop_reason != "tool_use" or not calls:
            return "".join(b.text for b in resp.content if b.type == "text")
        results = []
        for c in calls:
            if on_tool:
                on_tool(c.name, c.input)
            results.append({"type": "tool_result", "tool_use_id": c.id,
                            "content": json.dumps(run_tool(c.name, c.input), default=str)})
        messages.append({"role": "user", "content": results})
    return "I needed too many steps to answer that. Please try a more specific question."


class AIError(RuntimeError):
    """The model could not produce a usable answer (shown to the user as-is, never replaced by fake output)."""


def complete_json(system: str, user: str) -> dict:
    """One call to the active model that must return a JSON object."""
    act = active()
    if not act:
        raise AIError("No AI key configured. Add one under Settings in the sidebar.")
    provider, key, model = act
    if provider == "claude":
        import anthropic
        resp = anthropic.Anthropic(api_key=key).beta.messages.create(
            model=model, max_tokens=16000, system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": "low"},
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        )
        if resp.stop_reason == "refusal":
            raise AIError("The model declined this request.")
        if resp.stop_reason == "max_tokens":
            raise AIError("The model's answer was cut off. Try a shorter request.")
        text = "".join(b.text for b in resp.content if b.type == "text")
    else:
        from openai import OpenAI, BadRequestError
        client = OpenAI(api_key=key, base_url=PROVIDERS[provider]["base_url"])
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        try:
            resp = _create(client, provider, model=model, task="plan", messages=msgs, temperature=0.3,
                           response_format={"type": "json_object"})
        except BadRequestError as e:          # some models do not support JSON mode: ask in the prompt only
            if "response_format" not in str(e) and "json" not in str(e).lower():
                raise
            resp = _create(client, provider, model=model, task="plan", messages=msgs, temperature=0.3)
        text = resp.choices[0].message.content or ""
    return parse_json(text)


# Models that can read images, tried in order when the chosen chat model cannot (Groq's default Llama 3.3 is text-only)
VISION_MODELS = {"groq": ["llama-4-maverick", "llama-4-scout"], "openrouter": ["gemini", "llama-4", "qwen2.5-vl", "vl"]}


def vision_model(provider: str, key: str, model: str) -> str:
    """The active model if it can read images, otherwise the best vision model this key can use."""
    hints = VISION_MODELS.get(provider)
    if not hints or any(h in model.lower() for h in hints):
        return model           # Claude and Gemini models all read images
    from openai import OpenAI
    try:
        ids = [m.id for m in OpenAI(api_key=key, base_url=PROVIDERS[provider]["base_url"]).models.list()]
    except Exception:
        return model
    return next((i for h in hints for i in sorted(ids) if h in i.lower()), model)


def complete_vision(system: str, text: str, images: list[tuple[str, str]]) -> dict:
    """One call to the active model with images [(base64 data, media type)] that must return a JSON object."""
    act = active()
    if not act:
        raise AIError("No AI key configured. Add one under Settings in the sidebar.")
    provider, key, model = act
    if provider == "claude":
        import anthropic
        content = [{"type": "image", "source": {"type": "base64", "media_type": mt, "data": data}} for data, mt in images]
        resp = anthropic.Anthropic(api_key=key).beta.messages.create(
            model=model, max_tokens=16000, system=system,
            messages=[{"role": "user", "content": content + [{"type": "text", "text": text}]}],
            output_config={"effort": "low"},
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        )
        if resp.stop_reason == "refusal":
            raise AIError("The model declined to read this image.")
        if resp.stop_reason == "max_tokens":
            raise AIError("The list was too long to read in one go. Try fewer or smaller photos.")
        reply = "".join(b.text for b in resp.content if b.type == "text")
    else:
        from openai import BadRequestError, NotFoundError, OpenAI
        client = OpenAI(api_key=key, base_url=PROVIDERS[provider]["base_url"])
        model = vision_model(provider, key, model)
        content = [{"type": "text", "text": text}] + [
            {"type": "image_url", "image_url": {"url": f"data:{mt};base64,{data}"}} for data, mt in images]
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": content}]
        try:
            try:
                resp = _create(client, provider, model=model, task="vision", messages=msgs, temperature=0.1,
                               response_format={"type": "json_object"})
            except BadRequestError as e:      # some models do not support JSON mode: ask in the prompt only
                if "response_format" not in str(e) and "json" not in str(e).lower():
                    raise
                resp = _create(client, provider, model=model, task="vision", messages=msgs, temperature=0.1)
        except (BadRequestError, NotFoundError) as e:
            if any(w in str(e).lower() for w in ("image", "vision", "multimodal", "content type", "image_url")):
                raise AIError(f"The model {model} cannot read images. In Settings, choose Google Gemini or "
                              "Anthropic Claude, or a vision model such as Llama 4 on Groq.") from e
            raise
        reply = resp.choices[0].message.content or ""
    return parse_json(reply)


SPEECH_PROVIDERS = ("groq", "gemini")   # Claude has no audio input; OpenRouter's free models have none either
WHISPER = "whisper-large-v3"           # Groq's speech-to-text; detects Vietnamese, Chinese, English and more


def speech_provider() -> tuple[str, str, str] | None:
    """(provider, key, model) that can turn speech into text: the active one if it can,
    else a Groq or Gemini key configured on the server."""
    act = active()
    if act and act[0] in SPEECH_PROVIDERS:
        return act
    for p in SPEECH_PROVIDERS:
        if key := os.environ.get(PROVIDERS[p]["env"]):
            return p, key, PROVIDERS[p]["model"]
    return None


def transcribe(audio: bytes, hint: str = "") -> str:
    """Words spoken in a recording (WAV), in the language spoken. `hint`: product names that may come up,
    so names like "Teh Bags" are spelled the shop's way. The recording is sent to the provider, not stored."""
    sp = speech_provider()
    if not sp:
        raise AIError("Voice questions need a Groq or Google Gemini key (both free). "
                      "Add one under Settings in the sidebar, or type your question.")
    provider, key, model = sp
    from openai import OpenAI
    client = OpenAI(api_key=key, base_url=PROVIDERS[provider]["base_url"])
    if provider == "groq":
        extra = {"prompt": hint[:600]} if hint else {}
        text = client.audio.transcriptions.create(model=WHISPER, file=("question.wav", audio), temperature=0, **extra).text
    else:   # Gemini listens to audio inside a normal chat request
        import base64
        ask = ("Write down exactly what is said in this recording, in the language it is spoken in. "
               "Reply with the spoken words only, no quotes or comments."
               + (f" Product names that may be mentioned: {hint[:600]}" if hint else ""))
        resp = _create(client, provider, model=model, task="speech", temperature=0, messages=[{"role": "user", "content": [
            {"type": "text", "text": ask},
            {"type": "input_audio", "input_audio": {"data": base64.standard_b64encode(audio).decode("ascii"), "format": "wav"}}]}])
        text = resp.choices[0].message.content
    text = (text or "").strip().strip('"').strip()
    if not text:
        raise AIError("No words were heard. Try again, a little closer to the microphone.")
    return text


def parse_json(text: str) -> dict:
    """Extract the JSON object from a model reply (tolerates ```json fences or a sentence around it)."""
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        s = s.rsplit("```", 1)[0]
    start, end = s.find("{"), s.rfind("}")
    if start < 0 or end <= start:
        raise AIError("The model did not return a plan in the expected format. Please try again.")
    try:
        data = json.loads(s[start:end + 1])
    except json.JSONDecodeError as e:
        raise AIError(f"The model's answer was not valid JSON ({e.msg}). Please try again.") from e
    if not isinstance(data, dict):
        raise AIError("The model did not return a plan in the expected format. Please try again.")
    return data


def offline_answer() -> str:
    """Deterministic daily briefing used when no API key is configured."""
    r = advisor.recommendations()
    risks = advisor.risks_frame()
    lines = ["**Daily briefing** (AI chat is off — add a free Groq or Gemini key in the sidebar to ask questions)\n"]
    more = r[r["action"] == "Stock more"] if not r.empty else r
    for row in more.itertuples():
        lines.append(f"- **Stock more {row.product}**: order {row.order_low}–{row.order_high}. {row.reason}")
    if not risks.empty:
        lines.append("\n**Top risks**")
        for row in risks.head(5).itertuples():
            lines.append(f"- [{row.severity}] {row.product}: {row.message}")
    return "\n".join(lines)
