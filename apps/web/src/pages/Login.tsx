/**
 * Sign-in gate.
 *
 * Rendered instead of the application whenever the API reports that a session
 * is required and absent. It is the only screen that works unauthenticated, so
 * it deliberately shows nothing about the job search: no counts, no company
 * names, no profile.
 */

import { useState } from "react";
import { ApiError } from "../api/client";
import { useLogin, useSession } from "../api/hooks";

export function Login() {
  const { data: session } = useSession();
  const login = useLogin();
  const [password, setPassword] = useState("");

  const notConfigured = session ? !session.configured : false;

  return (
    <div className="login-shell">
      <form
        className="login-card"
        onSubmit={(event) => {
          event.preventDefault();
          login.mutate({ password });
        }}
      >
        <h1>Applyuminati</h1>

        {notConfigured ? (
          <>
            <p className="login-note">
              No password is set on this instance, so the API is refusing requests.
            </p>
            <p className="login-note">
              Set <code>APPLYUMINATI_SECURITY__PASSWORD</code> (or{" "}
              <code>security.password</code> in <code>config.toml</code>) and restart. To
              avoid a plaintext password in the environment, run{" "}
              <code>applyuminati auth hash-password</code> and use the hash it prints.
            </p>
          </>
        ) : (
          <>
            <label htmlFor="password">Password</label>
            <input
              id="password"
              type="password"
              autoComplete="current-password"
              autoFocus
              value={password}
              onChange={(event) => setPassword(event.target.value)}
            />
            <button type="submit" disabled={login.isPending || password.length === 0}>
              {login.isPending ? "Signing in…" : "Sign in"}
            </button>
          </>
        )}

        {login.error ? (
          <p className="login-error" role="alert">
            {login.error instanceof ApiError ? login.error.message : "sign-in failed"}
          </p>
        ) : null}

        {session?.listens_beyond_loopback ? (
          <p className="login-note">
            This server is reachable from other machines on the network. Put TLS in front
            of it before using it outside a trusted LAN.
          </p>
        ) : null}
      </form>
    </div>
  );
}
