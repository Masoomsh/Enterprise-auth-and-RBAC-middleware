from __future__ import annotations

import os
from typing import Any

import httpx
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

DEFAULT_API_URL = os.getenv("STREAMLIT_API_URL", "http://127.0.0.1:8011").rstrip("/")
# API key (gk_...) for the auth layer in front of the API; create one with POST /v1/api-keys.
DEFAULT_CREDENTIAL = os.getenv("STREAMLIT_API_KEY", "")
REQUEST_TIMEOUT = httpx.Timeout(300.0, connect=10.0)

st.set_page_config(
    page_title="Semantic Cache and Model Router",
    page_icon="🧠",
    layout="wide",
)


def api_request(
    method: str,
    path: str,
    *,
    api_url: str,
    headers: dict[str, str] | None = None,
    json: dict[str, Any] | None = None,
) -> httpx.Response:
    credential = st.session_state.get("api_credential") or DEFAULT_CREDENTIAL
    if credential:
        headers = {"Authorization": f"Bearer {credential}", **(headers or {})}
    response = httpx.request(
        method,
        f"{api_url.rstrip('/')}{path}",
        headers=headers,
        json=json,
        timeout=REQUEST_TIMEOUT,
    )
    if not response.is_success:
        detail = response.text
        try:
            detail = response.json().get("detail", detail)
        except ValueError:
            pass
        raise RuntimeError(f"API returned HTTP {response.status_code}: {detail}")
    return response


def initialize_state() -> None:
    st.session_state.setdefault("conversation", [])
    st.session_state.setdefault("last_request_id", None)
    st.session_state.setdefault("feedback_submitted", set())


def render_sidebar() -> tuple[str, bool, float, int]:
    st.sidebar.title("Connection and request options")
    api_url = st.sidebar.text_input("FastAPI URL", value=DEFAULT_API_URL).rstrip("/")
    st.session_state["api_credential"] = st.sidebar.text_input(
        "API key (gk_...)", value=DEFAULT_CREDENTIAL, type="password"
    ).strip()
    bypass_cache = st.sidebar.toggle("Bypass semantic cache", value=False)
    temperature = st.sidebar.slider(
        "Temperature",
        min_value=0.0,
        max_value=1.5,
        value=0.2,
        step=0.1,
        help="Higher temperatures make generated responses less deterministic.",
    )
    max_tokens = st.sidebar.number_input(
        "Maximum response tokens",
        min_value=1,
        max_value=32768,
        value=1024,
        step=128,
    )

    if st.sidebar.button("Check API readiness", use_container_width=True):
        try:
            health = api_request("GET", "/health/ready", api_url=api_url).json()
            st.sidebar.success(f"API status: {health.get('status', 'ready')}")
        except (httpx.RequestError, RuntimeError) as exc:
            st.sidebar.error(str(exc))

    if st.sidebar.button("Clear conversation", use_container_width=True):
        st.session_state.conversation = []
        st.session_state.last_request_id = None

    return api_url, bypass_cache, temperature, int(max_tokens)


def submit_message(
    prompt: str,
    api_url: str,
    bypass_cache: bool,
    temperature: float,
    max_tokens: int,
) -> None:
    messages = [
        {"role": item["role"], "content": item["content"]}
        for item in st.session_state.conversation
    ]
    messages.append({"role": "user", "content": prompt})
    headers: dict[str, str] = {}
    if bypass_cache:
        headers["x-cache-bypass"] = "true"
    payload = {
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    st.session_state.conversation.append({"role": "user", "content": prompt})

    try:
        with st.spinner("Routing your request and generating a response..."):
            response = api_request(
                "POST",
                "/v1/chat/completions",
                api_url=api_url,
                headers=headers,
                json=payload,
            )
        result = response.json()
        answer = result["choices"][0]["message"]["content"]
        request_id = response.headers.get("x-request-id", result.get("id"))
        metadata = {
            "request_id": request_id,
            "cache": response.headers.get("x-cache", "UNKNOWN"),
            "model": response.headers.get("x-routed-model", result.get("model", "unknown")),
            "category": response.headers.get("x-query-complexity", "unknown"),
            "experiment_arm": response.headers.get("x-experiment-arm", "unknown"),
            "usage": result.get("usage", {}),
        }
        st.session_state.conversation.append({
            "role": "assistant",
            "content": answer,
            "metadata": metadata,
        })
        st.session_state.last_request_id = request_id
    except (httpx.RequestError, RuntimeError, KeyError, IndexError, TypeError) as exc:
        st.error(f"Could not complete the request: {exc}")


def render_response_metadata(metadata: dict[str, Any]) -> None:
    columns = st.columns(4)
    columns[0].caption(f"Cache: **{metadata['cache']}**")
    columns[1].caption(f"Complexity: **{metadata['category']}**")
    columns[2].caption(f"Model: `{metadata['model']}`")
    columns[3].caption(f"Experiment: **{metadata['experiment_arm']}**")
    usage = metadata.get("usage") or {}
    if usage:
        st.caption(
            "Tokens: "
            f"{usage.get('prompt_tokens', 0)} prompt + "
            f"{usage.get('completion_tokens', 0)} completion"
        )


def render_conversation() -> None:
    conversation = st.session_state.conversation
    latest_user_index = next(
        (
            index
            for index in range(len(conversation) - 1, -1, -1)
            if conversation[index]["role"] == "user"
        ),
        None,
    )
    if latest_user_index is None:
        return

    latest_user = conversation[latest_user_index]
    latest_assistant = next(
        (
            item
            for item in conversation[latest_user_index + 1:]
            if item["role"] == "assistant"
        ),
        None,
    )

    with st.chat_message("user"):
        st.markdown(latest_user["content"])
    if latest_assistant is None:
        return

    with st.chat_message("assistant"):
        st.markdown(latest_assistant["content"])
        if latest_assistant.get("metadata"):
            render_response_metadata(latest_assistant["metadata"])


def render_architecture() -> None:
    st.subheader("How a request moves through the system")
    st.caption(
        "Each chat request is classified and routed. Semantic cache hits return "
        "immediately; misses call the selected Ollama model and save the result."
    )
    st.markdown(
        """
<div class="architecture-frame">
<svg viewBox="0 0 1120 600" role="img" aria-labelledby="architecture-title architecture-description"
     xmlns="http://www.w3.org/2000/svg">
  <title id="architecture-title">Semantic cache and intelligent model router architecture</title>
  <desc id="architecture-description">Streamlit sends a request to FastAPI. The router selects a model and Ollama creates an embedding. Pinecone searches for a similar cached response. Hits return immediately; misses call the selected model and store the response. SQLite powers the dashboard and experiment reports.</desc>
  <defs>
    <linearGradient id="header-gradient" x1="0" x2="1">
      <stop offset="0" stop-color="#635bff"/><stop offset="1" stop-color="#13b8a6"/>
    </linearGradient>
    <marker id="arrow" markerWidth="10" markerHeight="10" refX="8" refY="5" orient="auto">
      <path d="M0,0 L10,5 L0,10 z" fill="#718096"/>
    </marker>
    <marker id="arrow-hit" markerWidth="10" markerHeight="10" refX="8" refY="5" orient="auto">
      <path d="M0,0 L10,5 L0,10 z" fill="#159570"/>
    </marker>
  </defs>
  <rect x="1" y="1" width="1118" height="598" rx="22" fill="#f7f9ff" stroke="#e1e7f2"/>
  <text x="34" y="42" font-size="17" font-weight="700" fill="#334155">REQUEST PATH</text>
  <path d="M190 125 H245" stroke="#718096" stroke-width="3" marker-end="url(#arrow)"/>
  <path d="M410 125 H465" stroke="#718096" stroke-width="3" marker-end="url(#arrow)"/>
  <path d="M630 125 H685" stroke="#718096" stroke-width="3" marker-end="url(#arrow)"/>
  <path d="M845 125 H895" stroke="#159570" stroke-width="3" marker-end="url(#arrow-hit)"/>
  <path d="M765 170 V280" stroke="#718096" stroke-width="3" marker-end="url(#arrow)"/>
  <path d="M680 330 H605" stroke="#718096" stroke-width="3" marker-end="url(#arrow)"/>
  <path d="M440 330 H365" stroke="#718096" stroke-width="3" marker-end="url(#arrow)"/>
  <path d="M280 285 V185" stroke="#159570" stroke-width="3" marker-end="url(#arrow-hit)"/>
  <path d="M530 170 V475 H430" fill="none" stroke="#718096" stroke-width="2.5" marker-end="url(#arrow)"/>
  <path d="M270 495 H205 V170" fill="none" stroke="#718096" stroke-width="2.5" marker-end="url(#arrow)"/>
  <path d="M530 170 V495 H680" fill="none" stroke="#a78bfa" stroke-width="2.5" marker-end="url(#arrow)"/>

  <g>
    <rect x="30" y="80" width="160" height="90" rx="16" fill="white" stroke="#c7d2fe" stroke-width="2"/>
    <text x="110" y="116" text-anchor="middle" font-size="16" font-weight="700" fill="#312e81">Streamlit UI</text>
    <text x="110" y="143" text-anchor="middle" font-size="12" fill="#64748b">Chat · controls · reports</text>
  </g>
  <g>
    <rect x="245" y="80" width="165" height="90" rx="16" fill="white" stroke="#c7d2fe" stroke-width="2"/>
    <text x="327" y="116" text-anchor="middle" font-size="16" font-weight="700" fill="#312e81">FastAPI</text>
    <text x="327" y="143" text-anchor="middle" font-size="12" fill="#64748b">Validation · cache · API</text>
  </g>
  <g>
    <rect x="465" y="80" width="165" height="90" rx="16" fill="white" stroke="#99f6e4" stroke-width="2"/>
    <text x="547" y="116" text-anchor="middle" font-size="16" font-weight="700" fill="#115e59">Model router</text>
    <text x="547" y="143" text-anchor="middle" font-size="12" fill="#64748b">Complexity · A/B arm</text>
  </g>
  <g>
    <rect x="685" y="80" width="160" height="90" rx="16" fill="white" stroke="#99f6e4" stroke-width="2"/>
    <text x="765" y="116" text-anchor="middle" font-size="16" font-weight="700" fill="#115e59">Ollama embed</text>
    <text x="765" y="143" text-anchor="middle" font-size="12" fill="#64748b">Semantic vector</text>
  </g>
  <g>
    <rect x="895" y="80" width="190" height="90" rx="16" fill="white" stroke="#86efac" stroke-width="2"/>
    <text x="990" y="116" text-anchor="middle" font-size="16" font-weight="700" fill="#166534">Pinecone search</text>
    <text x="990" y="143" text-anchor="middle" font-size="12" fill="#64748b">Similarity · scope · TTL</text>
  </g>
  <text x="868" y="108" text-anchor="middle" font-size="11" font-weight="700" fill="#159570">HIT</text>
  <g>
    <rect x="680" y="285" width="170" height="90" rx="16" fill="white" stroke="#fed7aa" stroke-width="2"/>
    <text x="765" y="321" text-anchor="middle" font-size="16" font-weight="700" fill="#9a3412">Ollama chat</text>
    <text x="765" y="348" text-anchor="middle" font-size="12" fill="#64748b">Selected local model</text>
  </g>
  <g>
    <rect x="440" y="285" width="165" height="90" rx="16" fill="white" stroke="#fed7aa" stroke-width="2"/>
    <text x="522" y="321" text-anchor="middle" font-size="16" font-weight="700" fill="#9a3412">Response</text>
    <text x="522" y="348" text-anchor="middle" font-size="12" fill="#64748b">Text · tokens · metadata</text>
  </g>
  <g>
    <rect x="200" y="285" width="165" height="90" rx="16" fill="white" stroke="#86efac" stroke-width="2"/>
    <text x="282" y="321" text-anchor="middle" font-size="16" font-weight="700" fill="#166534">Pinecone upsert</text>
    <text x="282" y="348" text-anchor="middle" font-size="12" fill="#64748b">Store response + expiry</text>
  </g>
  <text x="780" y="235" font-size="11" font-weight="700" fill="#64748b">MISS</text>
  <text x="266" y="235" font-size="11" font-weight="700" fill="#159570">HIT returns answer</text>
  <g>
    <rect x="270" y="450" width="160" height="90" rx="16" fill="white" stroke="#cbd5e1" stroke-width="2"/>
    <text x="350" y="486" text-anchor="middle" font-size="16" font-weight="700" fill="#334155">Chat response</text>
    <text x="350" y="513" text-anchor="middle" font-size="12" fill="#64748b">Latest turn only shown</text>
  </g>
  <g>
    <rect x="680" y="450" width="190" height="90" rx="16" fill="white" stroke="#ddd6fe" stroke-width="2"/>
    <text x="775" y="486" text-anchor="middle" font-size="16" font-weight="700" fill="#5b21b6">SQLite analytics</text>
    <text x="775" y="513" text-anchor="middle" font-size="12" fill="#64748b">Cost · hit rate · feedback</text>
  </g>
  <text x="34" y="574" font-size="12" fill="#64748b">Solid green path: cache hit · Gray path: cache miss · Purple path: analytics reporting</text>
</svg>
</div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown("#### What each component does")
    columns = st.columns(4)
    columns[0].markdown(
        "**1 · Classify**\n\nRules label each prompt simple, reasoning, or coding."
    )
    columns[1].markdown(
        "**2 · Embed**\n\nOllama turns the prompt into a semantic vector."
    )
    columns[2].markdown(
        "**3 · Retrieve or generate**\n\nPinecone returns a matching response, or Ollama generates one."
    )
    columns[3].markdown(
        "**4 · Measure**\n\nFastAPI records usage, cache results, cost estimates, and feedback."
    )


def render_dashboard(api_url: str) -> None:
    st.subheader("Cache and cost dashboard")
    refresh_column, _ = st.columns([1, 5])
    refresh_column.button("Refresh dashboard", key="refresh_dashboard")
    try:
        report = api_request("GET", "/dashboard", api_url=api_url).json()
    except (httpx.RequestError, RuntimeError) as exc:
        st.warning(f"Could not load dashboard metrics: {exc}")
        return

    hit_rate = float(report.get("cache_hit_rate", 0))
    reduction = report.get("cost_reduction_percent")
    columns = st.columns(4)
    columns[0].metric("Cache hit rate", f"{hit_rate:.1%}")
    columns[1].metric(
        "Requests / cache hits",
        f"{report.get('requests', 0)} / {report.get('cache_hits', 0)}",
    )
    columns[2].metric("Routed cost", f"${report.get('actual_cost_usd', 0):.6f}")
    columns[3].metric(
        "Estimated cost reduction",
        "Unknown" if reduction is None else f"{reduction:.1f}%",
    )

    baseline = report.get("baseline_cost_usd", 0)
    savings = report.get("estimated_savings_usd", 0)
    st.caption(
        f"Baseline cost: ${baseline:.6f} · Estimated savings: ${savings:.6f}. "
        "Savings depend on configured model prices and measured traffic."
    )
    target = report.get("target_cost_reduction_percent", {})
    if target:
        st.caption(
            f"Target cost reduction: {target.get('minimum', 25)}–"
            f"{target.get('maximum', 40)}% (a target, not a guarantee)."
        )

    experiment = report.get("ab_test", {})
    if experiment:
        st.markdown("**A/B experiment by arm**")
        st.dataframe(
            [
                {
                    "Arm": arm,
                    "Requests": values.get("requests", 0),
                    "Average quality (1–5)": values.get("average_quality_score"),
                    "Routed cost (USD)": values.get("actual_cost_usd", 0),
                    "Baseline cost (USD)": values.get("baseline_cost_usd", 0),
                }
                for arm, values in experiment.items()
            ],
            hide_index=True,
            use_container_width=True,
        )


def render_feedback(api_url: str) -> None:
    request_id = st.session_state.last_request_id
    if not request_id or request_id in st.session_state.feedback_submitted:
        return
    st.subheader("Rate the latest response")
    with st.form("quality_feedback"):
        rating = st.slider("Response quality", min_value=1, max_value=5, value=4)
        submitted = st.form_submit_button("Submit quality rating")
    if submitted:
        try:
            api_request(
                "POST",
                "/v1/feedback",
                api_url=api_url,
                json={"request_id": request_id, "quality_score": rating},
            )
            st.session_state.feedback_submitted.add(request_id)
            st.success("Thank you. Your rating was recorded.")
        except (httpx.RequestError, RuntimeError) as exc:
            st.error(f"Could not submit feedback: {exc}")


initialize_state()
st.markdown(
    """
<style>
  .stApp { background: linear-gradient(180deg, #f8faff 0%, #ffffff 24rem); }
  .block-container { max-width: 1280px; padding-top: 2rem; padding-bottom: 3rem; }
  [data-testid="stSidebar"] { background: #f3f5fc; border-right: 1px solid #e6eaf2; }
  [data-testid="stMetric"] {
    background: #ffffff; border: 1px solid #e8ebf3; border-radius: 16px;
    padding: 1rem 1.1rem; box-shadow: 0 4px 16px rgba(34, 48, 82, .045);
  }
  .architecture-frame { overflow-x: auto; margin: .8rem 0 1.4rem; }
  .architecture-frame svg { display: block; min-width: 780px; width: 100%; height: auto; }
  div[data-testid="stChatMessage"] { border: 1px solid #edf0f7; border-radius: 16px; }
  div.stButton > button, div.stFormSubmitButton > button {
    border-radius: 10px; transition: transform .12s ease, box-shadow .12s ease;
  }
  div.stButton > button:hover, div.stFormSubmitButton > button:hover {
    transform: translateY(-1px); box-shadow: 0 5px 14px rgba(63, 81, 181, .12);
  }
  .hero-banner {
    padding: 1.65rem 1.8rem; border-radius: 20px; color: #fff;
    background: linear-gradient(115deg, #20265f 0%, #244d83 58%, #087c78 100%);
    box-shadow: 0 12px 32px rgba(30, 41, 90, .22); margin-bottom: 1.2rem;
  }
  .hero-eyebrow {
    color: #d7e8ff; font-size: .82rem; letter-spacing: .14em;
    font-weight: 750; margin-bottom: .55rem;
  }
  .hero-title {
    color: #fff; font-size: clamp(2.15rem, 4vw, 3.15rem);
    line-height: 1.16; font-weight: 800; letter-spacing: -.025em;
    text-shadow: 0 2px 12px rgba(0, 0, 0, .2); margin: 0 0 .65rem;
  }
  .hero-description {
    color: #f0f7ff; font-size: 1.08rem; line-height: 1.55;
    font-weight: 450; max-width: 760px;
  }
  @media (max-width: 640px) {
    .hero-banner { padding: 1.2rem; }
    .hero-title { font-size: 2rem; }
  }
</style>
<div class="hero-banner">
  <div class="hero-eyebrow">
    OLLAMA · PINECONE · FASTAPI
  </div>
  <div class="hero-title">
    Semantic Cache &amp; Model Router
  </div>
  <div class="hero-description">
    Ask once. Reuse meaningfully similar answers. Route each request to an appropriate model.
  </div>
</div>
    """,
    unsafe_allow_html=True,
)
api_url, bypass_cache, temperature, max_tokens = render_sidebar()

chat_tab, dashboard_tab, architecture_tab = st.tabs(
    ["💬  Chat", "📊  Dashboard", "🧩  Architecture"]
)
with chat_tab:
    st.subheader("Ask the model")
    st.caption("Only the latest question and answer are displayed. Earlier turns may still provide context.")
    prompt = st.chat_input("Ask a question")
    if prompt:
        submit_message(
            prompt,
            api_url,
            bypass_cache,
            temperature,
            max_tokens,
        )
    render_conversation()
    render_feedback(api_url)

with dashboard_tab:
    render_dashboard(api_url)

with architecture_tab:
    render_architecture()
