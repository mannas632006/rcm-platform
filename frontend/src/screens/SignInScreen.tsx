import { useState, type FormEvent } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";

import { Button } from "@/components/ui/button";
import { sessionEndedReason, signIn, useToken } from "@/lib/session";
import { useDocumentTitle } from "@/lib/useDocumentTitle";

/** A JWT's shape: three base64url parts. Checked here only to catch a bad paste. */
const JWT = /^[\w-]+\.[\w-]+\.[\w-]+$/;

/**
 * Development sign-in. Production signs in at the organisation's identity
 * provider (OIDC, S-3); until that is wired, a token minted by
 * `python -m tools.dev_token` is pasted here. The API refuses dev tokens
 * outside local and CI, so this screen cannot open a deployed system.
 */
export default function SignInScreen() {
  useDocumentTitle("Sign in");
  const token = useToken();
  const navigate = useNavigate();
  const location = useLocation();
  const [value, setValue] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const expired = sessionEndedReason() === "expired";
  const from = (location.state as { from?: string } | null)?.from ?? "/";

  if (token) return <Navigate to={from} replace />;

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    const pasted = value.trim();
    if (!pasted) {
      setProblem("Paste a token to sign in.");
      return;
    }
    if (!JWT.test(pasted)) {
      setProblem("That is not a token: it should be three dot-separated parts.");
      return;
    }
    signIn(pasted);
    navigate(from, { replace: true });
  }

  return (
    <div className="flex h-full items-center justify-center bg-surface p-6">
      <main className="w-full max-w-md rounded-panel border border-border bg-bg p-8">
        <h1 className="text-xl font-semibold">Sign in to RCM Platform</h1>
        {expired && (
          <p role="status" className="mt-4 rounded-control bg-warn-soft p-3 text-md text-warn-strong">
            Your session ended: the server no longer accepts your token. Sign in again to carry
            on.
          </p>
        )}
        {import.meta.env.PROD ? (
          <p className="mt-4 text-md">
            Sign-in is through your organisation&apos;s identity provider, which is not yet
            connected to this build.
          </p>
        ) : (
          <form onSubmit={onSubmit} noValidate className="mt-6 flex flex-col gap-4">
            <p className="text-md text-muted">
              Development only. Mint a token with{" "}
              <code className="rounded-control bg-surface px-1 text-text">
                python -m tools.dev_token --role AR_SPECIALIST
              </code>{" "}
              and paste it below.
            </p>
            <div className="flex flex-col gap-2">
              <label htmlFor="token" className="text-md font-medium">
                Bearer token
              </label>
              <textarea
                id="token"
                value={value}
                onChange={(event) => {
                  setValue(event.target.value);
                  setProblem(null);
                }}
                rows={4}
                spellCheck={false}
                autoComplete="off"
                aria-invalid={problem !== null}
                aria-describedby={problem ? "token-problem" : undefined}
                className="rounded-control border border-border-control p-2 font-mono text-sm"
              />
              {problem && (
                <p id="token-problem" className="text-md text-danger">
                  {problem}
                </p>
              )}
            </div>
            <Button type="submit">Sign in</Button>
          </form>
        )}
      </main>
    </div>
  );
}
