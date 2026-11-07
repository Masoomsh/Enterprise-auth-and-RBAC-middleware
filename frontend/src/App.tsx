import { useEffect, useState } from "react";
import {
  BrowserRouter,
  Link,
  Navigate,
  Route,
  Routes,
} from "react-router-dom";
import { signOut, useSessionContext } from "supertokens-auth-react/recipe/session";
import { getSuperTokensRoutesForReactRouterDom } from "supertokens-auth-react/ui";
import { EmailPasswordPreBuiltUI } from "supertokens-auth-react/recipe/emailpassword/prebuiltui";
import * as ReactRouterDOM from "react-router-dom";
import SemanticCache from "./SemanticCache";

type CurrentUser = {
  user_id: string;
};

type SessionError = {
  userId: string;
  message: string;
};

function Home() {
  const session = useSessionContext();
  const sessionUserId =
    !session.loading && session.doesSessionExist ? session.userId : null;
  const [user, setUser] = useState<CurrentUser | null>(null);
  const [error, setError] = useState<SessionError | null>(null);

  useEffect(() => {
    if (sessionUserId === null) {
      return undefined;
    }

    const userId = sessionUserId;
    const controller = new AbortController();

    async function loadCurrentUser() {
      try {
        const response = await fetch(
          `${import.meta.env.VITE_API_DOMAIN}/api/me`,
          {
            credentials: "include",
            signal: controller.signal,
          },
        );

        if (!response.ok) {
          throw new Error(`Gateway returned HTTP ${response.status}.`);
        }

        setUser((await response.json()) as CurrentUser);
        setError(null);
      } catch (requestError) {
        if (controller.signal.aborted) {
          return;
        }

        setError({
          userId,
          message:
            requestError instanceof Error
              ? requestError.message
              : "Could not verify the session with the gateway.",
        });
      }
    }

    void loadCurrentUser();
    return () => controller.abort();
  }, [sessionUserId]);

  if (session.loading) {
    return <p className="status">Checking your session…</p>;
  }

  // Signed in: go straight to the semantic cache screen.
  if (session.doesSessionExist) {
    return <Navigate to="/app" replace />;
  }

  return (
    <main className="page">
      <section className="card">
        <p className="eyebrow">ENTERPRISE AUTH GATEWAY</p>
        <h1>Welcome</h1>
        {session.doesSessionExist ? (
          <>
            <p className="status">
              {error?.userId === sessionUserId
                ? `Session exists, but gateway verification failed: ${error.message}`
                : user?.user_id === sessionUserId
                ? `Signed in as ${user.user_id}`
                : "Verifying your session with the gateway…"}
            </p>
            <button
              className="button secondary"
              onClick={() => void signOut()}
              type="button"
            >
              Sign out
            </button>
          </>
        ) : (
          <>
            <p className="status">Sign in to continue to the gateway.</p>
            <Link className="button" to="/auth">
              Sign in or create an account
            </Link>
          </>
        )}
      </section>
    </main>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        {getSuperTokensRoutesForReactRouterDom(ReactRouterDOM, [
          EmailPasswordPreBuiltUI,
        ])}
        <Route path="/" element={<Home />} />
        <Route path="/app" element={<SemanticCache />} />
      </Routes>
    </BrowserRouter>
  );
}
