import { useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams, Link } from "react-router-dom";
import { useAuth } from "../auth/useAuth";
import api from "../auth/api";
import { Logo } from "../components/brand";

/**
 * Extension connect / handshake page.
 *
 * Opened by the Chrome extension via chrome.identity.launchWebAuthFlow. Reuses
 * the live web session to mint a one-time PKCE authorization code, then redirects
 * back to the extension's chromiumapp.org URL with the code in the fragment. The
 * extension exchanges it for tokens at POST /auth/extension/token.
 *
 * No credentials are ever entered here, if the user isn't signed in, we bounce
 * through the normal /sign-in UI (?next) and return.
 */

type Status = "checking" | "authorizing" | "done" | "error" | "needs_verification" | "bad_request";

function isExtensionRedirect(uri: string | null): uri is string {
  if (!uri) return false;
  try {
    const u = new URL(uri);
    return u.protocol === "https:" && u.hostname.endsWith(".chromiumapp.org");
  } catch {
    return false;
  }
}

export default function ExtensionConnect() {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const { isAuthenticated, isLoading, isEmailVerified } = useAuth();

  const [status, setStatus] = useState<Status>("checking");
  const [errorMsg, setErrorMsg] = useState("");
  const started = useRef(false); // guard against React StrictMode double-run

  const redirectUri = params.get("redirect_uri");
  const codeChallenge = params.get("code_challenge");
  const state = params.get("state") ?? "";

  useEffect(() => {
    if (isLoading) return;

    // Validate the request shape first.
    if (!isExtensionRedirect(redirectUri) || !codeChallenge) {
      setStatus("bad_request");
      return;
    }

    // Not signed in → reuse the normal sign-in UI, then come back here.
    if (!isAuthenticated) {
      const here = `/extension/connect?${params.toString()}`;
      navigate(`/sign-in?next=${encodeURIComponent(here)}`, { replace: true });
      return;
    }

    // Signed in but email not verified, authorize requires a verified user.
    if (!isEmailVerified) {
      setStatus("needs_verification");
      return;
    }

    if (started.current) return;
    started.current = true;

    setStatus("authorizing");
    api
      .post("/auth/extension/authorize", {
        code_challenge: codeChallenge,
        code_challenge_method: "S256",
        redirect_uri: redirectUri,
      })
      .then(({ data }) => {
        const code = data.code as string;
        const target =
          `${redirectUri}#code=${encodeURIComponent(code)}&state=${encodeURIComponent(state)}`;
        setStatus("done");
        // Hand the code back to the extension. launchWebAuthFlow intercepts the
        // chromiumapp.org redirect and closes this window automatically.
        window.location.replace(target);
      })
      .catch((err: unknown) => {
        let detail = "Could not connect the extension. Please try again.";
        if (err && typeof err === "object" && "response" in err) {
          const ax = err as { response?: { data?: { detail?: string } } };
          if (ax.response?.data?.detail) detail = ax.response.data.detail;
        }
        setErrorMsg(detail);
        setStatus("error");
      });
  }, [isLoading, isAuthenticated, isEmailVerified, redirectUri, codeChallenge, state, params, navigate]);

  return (
    <div className="auth-page">
      <div className="auth-card" style={{ textAlign: "center" }}>
        <div className="auth-brand">
          <Logo variant="mark" theme="light" size={44} className="auth-brand-logo" />
        </div>

        {(status === "checking" || status === "authorizing") && (
          <>
            <h1 className="auth-title">Connecting your extension…</h1>
            <p className="auth-subtitle">Securely linking your Tailrd account. One moment.</p>
            <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-indigo-600"
                 style={{ margin: "1.5rem auto 0" }} />
          </>
        )}

        {status === "done" && (
          <>
            <h1 className="auth-title">You're connected!</h1>
            <p className="auth-subtitle">
              You can close this tab and return to the Tailrd extension.
            </p>
          </>
        )}

        {status === "needs_verification" && (
          <>
            <h1 className="auth-title">Verify your email first</h1>
            <p className="auth-subtitle">
              Confirm your email address, then reopen the extension to connect.
            </p>
            <Link to="/verify-email" className="auth-submit" style={{ display: "inline-block", marginTop: "1.25rem" }}>
              Verify email
            </Link>
          </>
        )}

        {status === "error" && (
          <>
            <h1 className="auth-title">Connection failed</h1>
            <div className="auth-error" role="alert" style={{ marginTop: "1rem" }}>{errorMsg}</div>
            <button
              className="auth-submit"
              style={{ marginTop: "1rem" }}
              onClick={() => {
                started.current = false;
                setStatus("checking");
              }}
            >
              Try again
            </button>
          </>
        )}

        {status === "bad_request" && (
          <>
            <h1 className="auth-title">Open this from the extension</h1>
            <p className="auth-subtitle">
              This page links the Tailrd browser extension to your account. Click
              “Connect your Tailrd account” inside the extension to start.
            </p>
          </>
        )}
      </div>
    </div>
  );
}
