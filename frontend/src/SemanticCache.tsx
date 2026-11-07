import { type FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { Navigate } from "react-router-dom";
import { signOut, useSessionContext } from "supertokens-auth-react/recipe/session";

const API = import.meta.env.VITE_API_DOMAIN;

type Turn = {
  role: "user" | "assistant";
  content: string;
  meta?: { cache: string; model: string; category: string; requestId: string; tokens: number };
};

type Dashboard = {
  requests: number;
  cache_hits: number;
  cache_hit_rate: number;
  actual_cost_usd: number;
  estimated_savings_usd?: number;
};

// Same-origin cookies are sent with credentials: "include"; SuperTokens patches fetch to refresh
// the session transparently.
async function api(path: string, init?: RequestInit): Promise<Response> {
  return fetch(`${API}${path}`, { credentials: "include", ...init });
}

async function failure(response: Response): Promise<string> {
  if (response.status === 403) return "Your role is not permitted to do that (403).";
  if (response.status === 401) return "Your session is not valid. Sign in again (401).";
  try {
    const body = await response.json();
    return `${response.status}: ${body.detail ?? JSON.stringify(body)}`;
  } catch {
    return `HTTP ${response.status}`;
  }
}

export default function SemanticCache() {
  const session = useSessionContext();
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState("");
  const [bypass, setBypass] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dashboard, setDashboard] = useState<Dashboard | null>(null);
  const [dashboardNote, setDashboardNote] = useState<string | null>(null);
  const [rated, setRated] = useState<Set<string>>(new Set());
  const endRef = useRef<HTMLDivElement>(null);

  const loadDashboard = useCallback(async () => {
    try {
      const response = await api("/dashboard");
      if (!response.ok) {
        setDashboard(null);
        setDashboardNote(await failure(response));
        return;
      }
      setDashboard((await response.json()) as Dashboard);
      setDashboardNote(null);
    } catch {
      setDashboardNote("Could not reach the gateway.");
    }
  }, []);

  const signedIn = !session.loading && session.doesSessionExist;
  useEffect(() => {
    if (!signedIn) return undefined;
    const first = setTimeout(() => void loadDashboard(), 0);
    const timer = setInterval(() => void loadDashboard(), 15000);
    return () => {
      clearTimeout(first);
      clearInterval(timer);
    };
  }, [signedIn, loadDashboard]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [turns]);

  if (session.loading) return <p className="status">Checking your session…</p>;
  if (!session.doesSessionExist) return <Navigate to="/auth" replace />;

  async function send(event: FormEvent) {
    event.preventDefault();
    const text = input.trim();
    if (!text || busy) return;
    const history = [...turns, { role: "user" as const, content: text }];
    setTurns(history);
    setInput("");
    setBusy(true);
    setError(null);
    try {
      const response = await api("/v1/chat/completions", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(bypass ? { "x-cache-bypass": "true" } : {}),
        },
        body: JSON.stringify({
          messages: history.map(({ role, content }) => ({ role, content })),
        }),
      });
      if (!response.ok) {
        setError(await failure(response));
        return;
      }
      const data = await response.json();
      setTurns([
        ...history,
        {
          role: "assistant",
          content: data.choices[0].message.content,
          meta: {
            cache: response.headers.get("x-cache") ?? "UNKNOWN",
            model: response.headers.get("x-routed-model") ?? data.model,
            category: response.headers.get("x-query-complexity") ?? "",
            requestId: data.id,
            tokens: data.usage?.total_tokens ?? 0,
          },
        },
      ]);
      void loadDashboard();
    } catch {
      setError("Could not reach the gateway.");
    } finally {
      setBusy(false);
    }
  }

  async function rate(requestId: string, score: number) {
    const response = await api("/v1/feedback", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ request_id: requestId, quality_score: score }),
    });
    if (response.ok) setRated(new Set(rated).add(requestId));
    else setError(await failure(response));
  }

  return (
    <main className="sc-page">
      <header className="sc-header">
        <div>
          <p className="eyebrow">ENTERPRISE AUTH GATEWAY</p>
          <h1 className="sc-title">Semantic Cache</h1>
          <p className="status sc-sub">Signed in as {session.userId}</p>
        </div>
        <div className="sc-actions">
          <button
            className="button secondary"
            onClick={() => {
              setTurns([]);
              setError(null);
              setRated(new Set());
            }}
            type="button"
          >
            New chat
          </button>
          <button className="button secondary" onClick={() => void signOut()} type="button">
            Sign out
          </button>
        </div>
      </header>

      <section className="sc-stats" aria-label="Cache statistics">
        {dashboard ? (
          <>
            <div className="sc-stat">Requests<b>{dashboard.requests}</b></div>
            <div className="sc-stat">Cache hits<b>{dashboard.cache_hits}</b></div>
            <div className="sc-stat">Hit rate<b>{(dashboard.cache_hit_rate * 100).toFixed(0)}%</b></div>
            <div className="sc-stat">Routed cost<b>${dashboard.actual_cost_usd.toFixed(4)}</b></div>
          </>
        ) : (
          <p className="status sc-note">Statistics unavailable. {dashboardNote}</p>
        )}
      </section>

      <section className="sc-chat" aria-label="Conversation">
        {turns.length === 0 && (
          <p className="status">
            Ask something. To see a cache HIT, click New chat and ask the same question again (a follow-up in the same chat is a different prompt).
          </p>
        )}
        {turns.map((turn, index) => (
          <div key={index} className={`sc-turn ${turn.role}`}>
            <div className="sc-bubble">{turn.content}</div>
            {turn.meta && (
              <div className="sc-meta">
                <span className={`sc-badge ${turn.meta.cache.toLowerCase()}`}>{turn.meta.cache}</span>
                <span>{turn.meta.model}</span>
                {turn.meta.category && <span>{turn.meta.category}</span>}
                <span>{turn.meta.tokens} tokens</span>
                {rated.has(turn.meta.requestId) ? (
                  <span>Thanks for the feedback</span>
                ) : (
                  <span className="sc-rate">
                    Rate:
                    {[1, 2, 3, 4, 5].map((score) => (
                      <button
                        key={score}
                        type="button"
                        onClick={() => void rate(turn.meta!.requestId, score)}
                        aria-label={`Rate ${score} out of 5`}
                      >
                        {score}
                      </button>
                    ))}
                  </span>
                )}
              </div>
            )}
          </div>
        ))}
        {busy && <p className="status">Routing your request…</p>}
        <div ref={endRef} />
      </section>

      {error && <p className="sc-error" role="alert">{error}</p>}

      <form className="sc-form" onSubmit={(event) => void send(event)}>
        <input
          value={input}
          onChange={(event) => setInput(event.target.value)}
          placeholder="Type a message"
          aria-label="Message"
          disabled={busy}
        />
        <label className="sc-bypass">
          <input type="checkbox" checked={bypass} onChange={(e) => setBypass(e.target.checked)} />
          Bypass cache
        </label>
        <button className="button" type="submit" disabled={busy || !input.trim()}>
          Send
        </button>
      </form>
    </main>
  );
}
