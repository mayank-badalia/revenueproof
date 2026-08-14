"use client";

/**
 * Sign in or register.
 *
 * A split screen: the product's claim on the navy side, the form on the paper side.
 * The left panel states what the tool does in one sentence and then shows the chain
 * it verifies, because that chain *is* the product and someone arriving at a login
 * screen has no other way to find out what they are signing in to.
 */

import { useState } from "react";

import { ApiError, api, setToken } from "@/lib/api";
import { Banner, Button, Field, Input, Spinner } from "@/components/ui/primitives";

const CHAIN = ["Customer", "Contract", "Invoice", "Payment", "Bank receipt", "Refund"];

export function AuthGate({ onAuthenticated }: { onAuthenticated: () => void }) {
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [fullName, setFullName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const response =
        mode === "login"
          ? await api.login(email, password)
          : await api.register(email, password, fullName);
      setToken(response.access_token);
      onAuthenticated();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Something went wrong");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-screen">
      <div className="relative hidden w-[46%] flex-col justify-between overflow-hidden bg-navy-900 p-11 text-white lg:flex">
        <div className="flex items-center gap-2.5">
          <span className="grid h-8 w-8 place-items-center rounded-[7px] bg-white/10 font-mono text-[11px] font-semibold ring-1 ring-white/15">
            RP
          </span>
          <span className="text-[15px] font-semibold tracking-[-0.01em]">RevenueProof</span>
        </div>

        <div className="max-w-[400px]">
          <h1 className="text-[30px] font-semibold leading-[1.18] tracking-[-0.025em]">
            Does the revenue a startup claims actually exist?
          </h1>
          <p className="mt-3.5 text-[14px] leading-relaxed text-white/60">
            Every rupee of real revenue leaves the same trail. RevenueProof rebuilds that
            trail from five systems and shows you which links are missing.
          </p>

          <ol className="mt-8 space-y-0">
            {CHAIN.map((step, index) => (
              <li key={step} className="flex items-center gap-3">
                <span className="flex flex-col items-center">
                  <span className="h-[7px] w-[7px] rounded-full bg-white/35" />
                  {index < CHAIN.length - 1 && <span className="h-6 w-px bg-white/15" />}
                </span>
                <span className="-mt-[1px] pb-[9px] text-[13px] text-white/75">{step}</span>
              </li>
            ))}
          </ol>
        </div>

        <p className="max-w-[400px] text-[11.5px] leading-relaxed text-white/35">
          Shows what the evidence supports and what it does not. Not investment advice,
          and it does not certify revenue.
        </p>
      </div>

      <div className="flex flex-1 items-center justify-center px-6 py-12">
        <div className="w-full max-w-[352px]">
          <h2 className="text-[21px] font-semibold tracking-[-0.02em] text-ink">
            {mode === "login" ? "Sign in" : "Create an account"}
          </h2>
          <p className="mt-1 text-[13px] text-ink-2">
            {mode === "login"
              ? "Continue to your verification workspaces."
              : "Your decisions are recorded against this identity."}
          </p>

          <form onSubmit={submit} className="mt-6 space-y-3.5">
            {mode === "register" && (
              <Field label="Full name">
                <Input
                  value={fullName}
                  onChange={(e) => setFullName(e.target.value)}
                  placeholder="Jane Doe"
                  autoComplete="name"
                />
              </Field>
            )}
            <Field label="Email">
              <Input
                type="email"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="you@company.com"
                autoComplete="email"
              />
            </Field>
            <Field label="Password">
              <Input
                type="password"
                required
                minLength={8}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="At least 8 characters"
                autoComplete={mode === "login" ? "current-password" : "new-password"}
              />
            </Field>

            {error && <Banner tone="error">{error}</Banner>}

            <Button
              type="submit"
              variant="primary"
              className="w-full"
              disabled={busy}
              icon={busy ? <Spinner /> : undefined}
            >
              {busy ? "Please wait…" : mode === "login" ? "Sign in" : "Create account"}
            </Button>
          </form>

          <p className="mt-5 text-[12.5px] text-ink-2">
            {mode === "login" ? "No account yet?" : "Already have an account?"}{" "}
            <button
              onClick={() => {
                setMode(mode === "login" ? "register" : "login");
                setError(null);
              }}
              className="font-medium text-cobalt hover:underline"
            >
              {mode === "login" ? "Create one" : "Sign in"}
            </button>
          </p>
        </div>
      </div>
    </div>
  );
}
