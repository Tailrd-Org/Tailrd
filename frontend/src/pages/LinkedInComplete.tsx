import { useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams, Link } from "react-router-dom";
import { useAuth } from "../auth/useAuth";
import { safeNextPath } from "../auth/nextRedirect";
import { Logo } from "../components/brand";

/** Landing page after the LinkedIn OAuth callback (route: /linkedin/complete).
 *  Hydrates the session from the refresh cookie, then redirects into the app. */
export default function LinkedInComplete() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const { completeOAuthRedirect } = useAuth();
  const [failed, setFailed] = useState(false);
  const ranRef = useRef(false);

  useEffect(() => {
    if (ranRef.current) return;
    ranRef.current = true;
    completeOAuthRedirect()
      .then(() => navigate(safeNextPath(searchParams.get("next")), { replace: true }))
      .catch(() => setFailed(true));
  }, [completeOAuthRedirect, navigate, searchParams]);

  return (
    <div className="auth-page">
      <div className="auth-card">
        <div className="auth-brand">
          <Logo variant="mark" theme="light" size={44} className="auth-brand-logo" />
        </div>
        {failed ? (
          <div className="auth-head">
            <h1 className="auth-title">Sign-in failed</h1>
            <p className="auth-subtitle">We couldn't complete your LinkedIn sign-in.</p>
            <Link to="/sign-in" className="auth-link">Back to sign in</Link>
          </div>
        ) : (
          <div className="auth-head">
            <h1 className="auth-title">Signing you in…</h1>
            <p className="auth-subtitle">One moment while we finish connecting your LinkedIn account.</p>
          </div>
        )}
      </div>
    </div>
  );
}
