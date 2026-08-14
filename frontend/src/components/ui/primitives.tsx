"use client";

/**
 * The small shared pieces. Kept in one file because there are few of them and each
 * is a handful of lines — a folder of one-component files would be filing, not
 * structure.
 */

import type { ButtonHTMLAttributes, InputHTMLAttributes, ReactNode } from "react";

type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";

const BUTTON: Record<ButtonVariant, string> = {
  primary:
    "bg-cobalt text-white hover:bg-cobalt/90 disabled:bg-cobalt/40 disabled:text-white/70",
  secondary:
    "bg-paper text-ink ring-1 ring-line hover:bg-slate-soft disabled:text-ink-3 disabled:hover:bg-paper",
  ghost: "text-ink-2 hover:bg-slate-soft disabled:text-ink-3",
  danger: "bg-rust text-white hover:bg-rust/90 disabled:bg-rust/40",
};

export function Button({
  variant = "secondary",
  size = "md",
  icon,
  children,
  className = "",
  ...props
}: {
  variant?: ButtonVariant;
  size?: "sm" | "md";
  icon?: ReactNode;
} & ButtonHTMLAttributes<HTMLButtonElement>) {
  const sizing = size === "sm" ? "h-7 px-2.5 text-[12px]" : "h-9 px-3.5 text-[13px]";
  return (
    <button
      {...props}
      className={`inline-flex shrink-0 items-center justify-center gap-1.5 rounded-[7px] font-medium transition-colors disabled:cursor-not-allowed ${sizing} ${BUTTON[variant]} ${className}`}
    >
      {icon}
      {children}
    </button>
  );
}

export function Field({
  label,
  hint,
  children,
  className = "",
}: {
  label: string;
  hint?: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <label className={`block ${className}`}>
      <span className="mb-1.5 block text-[12px] font-medium text-ink-2">{label}</span>
      {children}
      {hint && <span className="mt-1 block text-[11.5px] text-ink-3">{hint}</span>}
    </label>
  );
}

export function Input({
  className = "",
  ...props
}: InputHTMLAttributes<HTMLInputElement>) {
  return (
    <input
      {...props}
      className={`h-9 w-full rounded-[7px] border border-line bg-paper px-3 text-[13.5px] text-ink placeholder:text-ink-3 focus:border-cobalt focus:outline-none focus:ring-2 focus:ring-cobalt/15 ${className}`}
    />
  );
}

/** A short caption above a group. Encodes hierarchy, not decoration. */
export function Eyebrow({ children }: { children: ReactNode }) {
  return (
    <div className="text-[10.5px] font-semibold uppercase tracking-[0.09em] text-ink-3">
      {children}
    </div>
  );
}

export function Card({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={`rounded-[10px] border border-line bg-paper ${className}`}>
      {children}
    </div>
  );
}

/** A definition row: label left, value right, hairline between. */
export function Row({
  label,
  children,
  mono = false,
}: {
  label: string;
  children: ReactNode;
  mono?: boolean;
}) {
  return (
    <div className="flex items-baseline justify-between gap-4 border-b border-line-2 py-2 last:border-b-0">
      <span className="shrink-0 text-[12.5px] text-ink-2">{label}</span>
      <span
        className={`min-w-0 truncate text-right text-[12.5px] text-ink ${
          mono ? "font-mono tnum" : ""
        }`}
      >
        {children}
      </span>
    </div>
  );
}

export function Spinner({ className = "" }: { className?: string }) {
  return (
    <svg
      className={`animate-spin ${className}`}
      width="13"
      height="13"
      viewBox="0 0 16 16"
      fill="none"
      aria-hidden
    >
      <circle cx="8" cy="8" r="6.4" stroke="currentColor" strokeWidth="2" opacity="0.22" />
      <path
        d="M14.4 8A6.4 6.4 0 0 0 8 1.6"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
      />
    </svg>
  );
}

export function Banner({
  tone = "info",
  children,
}: {
  tone?: "info" | "warn" | "error" | "success";
  children: ReactNode;
}) {
  const tones = {
    info: "bg-cobalt-soft text-cobalt ring-cobalt/15",
    warn: "bg-amber-soft text-amber ring-amber/20",
    error: "bg-rust-soft text-rust ring-rust/20",
    success: "bg-emerald-soft text-emerald ring-emerald/20",
  } as const;
  return (
    <div
      className={`rounded-[7px] px-3 py-2 text-[12.5px] leading-relaxed ring-1 ${tones[tone]}`}
    >
      {children}
    </div>
  );
}

export function DownloadIcon({ size = 13 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none" aria-hidden>
      <path
        d="M8 2v8m0 0 3-3m-3 3L5 7M2.5 12.5h11"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function PlayIcon({ size = 13 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none" aria-hidden>
      <path d="M4.5 3.2v9.6l8-4.8-8-4.8Z" fill="currentColor" />
    </svg>
  );
}

export function LockIcon({ size = 12 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none" aria-hidden>
      <rect x="3.2" y="7" width="9.6" height="6.4" rx="1.4" stroke="currentColor" strokeWidth="1.4" />
      <path d="M5.6 7V5.2a2.4 2.4 0 0 1 4.8 0V7" stroke="currentColor" strokeWidth="1.4" />
    </svg>
  );
}

export function CheckIcon({ size = 12 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none" aria-hidden>
      <path
        d="M3.2 8.4 6.4 11.6l6.4-7.2"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
