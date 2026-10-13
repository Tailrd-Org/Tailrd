import { useState, FormEvent } from "react";
import { useNavigate, Link, useSearchParams } from "react-router-dom";
import { Envelope, Lock, Eye, EyeSlash } from "@phosphor-icons/react";
import { useAuth } from "../auth/useAuth";
import { GoogleSignInButton } from "../auth/GoogleSignInButton";
import { LinkedInSignInButton } from "../auth/LinkedInSignInButton";
import { safeNextPath } from "../auth/nextRedirect";
import { Logo } from "../components/brand";

export default function SignInPage() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const { login } = useAuth();

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError("");
    setIsSubmitting(true);

    try {
      await login(email, password);
      navigate(safeNextPath(searchParams.get("next")));
    } catch (err: unknown) {
      if (err && typeof err === "object" && "response" in err) {
        const axiosErr = err as { response?: { data?: { detail?: string }; status?: number } };
        const status = axiosErr.response?.status;
        const detail = axiosErr.response?.data?.detail;
        if (status === 401) {
          setError(detail || "Invalid email or password.");
        } else if (status === 422) {
          setError(detail || "Please check your input and try again.");
        } else {
          setError(detail || `Server error (${status}). Please try again later.`);
        }
      } else if (err && typeof err === "object" && "code" in err) {
        const axiosErr = err as { code?: string; message?: string };
        if (axiosErr.code === "ERR_NETWORK") {
          setError("Cannot reach the server. Check that the backend is running.");
        } else {
          setError(axiosErr.message || "Network error. Please try again.");
        }
      } else {
        setError("Login failed. Please try again.");
      }
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <div className="auth-page">
      <div className="auth-card">
        <div className="auth-brand">
          <Logo variant="mark" theme="light" size={44} className="auth-brand-logo" />
        </div>

        <div className="auth-head">
          <h1 className="auth-title">Sign in to Tailrd</h1>
          <p className="auth-subtitle">Welcome back. Let's find your next role.</p>
        </div>

        <form onSubmit={handleSubmit} className="auth-form">
          {error && (
            <div className="auth-error" role="alert">
              {error}
            </div>
          )}

          <div className="auth-field">
            <label htmlFor="email" className="auth-label">
              Email
            </label>
            <div className="auth-input-wrap">
              <Envelope className="auth-input-icon" size={18} weight="regular" />
              <input
                id="email"
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                className="auth-input"
                placeholder="you@example.com"
                required
                autoComplete="email"
              />
            </div>
          </div>

          <div className="auth-field">
            <label htmlFor="password" className="auth-label">
              Password
            </label>
            <div className="auth-input-wrap">
              <Lock className="auth-input-icon" size={18} weight="regular" />
              <input
                id="password"
                type={showPassword ? "text" : "password"}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="auth-input"
                placeholder="Enter your password"
                required
                autoComplete="current-password"
              />
              <button
                type="button"
                className="auth-input-toggle"
                onClick={() => setShowPassword((v) => !v)}
                aria-label={showPassword ? "Hide password" : "Show password"}
                tabIndex={-1}
              >
                {showPassword ? <EyeSlash size={18} /> : <Eye size={18} />}
              </button>
            </div>
          </div>

          <button
            type="submit"
            className="auth-submit"
            disabled={isSubmitting}
          >
            {isSubmitting ? "Signing in..." : "Sign in"}
          </button>
        </form>

        <GoogleSignInButton />
        <LinkedInSignInButton />

        <p className="auth-footer">
          Don't have an account?{" "}
          <Link to={{ pathname: "/sign-up", search: searchParams.toString() }} className="auth-link">
            Sign up
          </Link>
        </p>
      </div>
    </div>
  );
}
