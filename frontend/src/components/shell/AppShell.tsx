"use client";

/**
 * The frame every screen sits in: a navy rail on the left, a thin bar across the top.
 *
 * The rail is deep navy and the canvas beside it is warm off-white, so the working
 * surface reads as the lit part of the screen and the chrome recedes. Navigation is
 * four destinations and no more — a diligence tool with a crowded sidebar is telling
 * you it does not know what it is for.
 */

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";

import type { Workspace } from "@/lib/types";

const NAV = [
  { href: "/", label: "Dashboard", icon: DashboardIcon },
  { href: "/workspaces", label: "Workspaces", icon: FolderIcon },
  { href: "/reports", label: "Reports", icon: ChartIcon },
  { href: "/audit", label: "Audit log", icon: ListIcon },
];

export function AppShell({
  children,
  workspaces = [],
  activeWorkspaceId,
  email,
  onSignOut,
  topBar,
}: {
  children: ReactNode;
  workspaces?: Workspace[];
  activeWorkspaceId?: string;
  email?: string | null;
  onSignOut?: () => void;
  topBar?: ReactNode;
}) {
  const pathname = usePathname();
  /* Below the lg breakpoint the rail slides over the working surface instead of
     taking a fifth of it. A 236px fixed column on a phone leaves the canvas about
     130px wide, which is not a narrow version of this product — it is an unusable
     one. */
  const [navOpen, setNavOpen] = useState(false);
  useEffect(() => setNavOpen(false), [pathname]);

  return (
    <div className="flex h-screen overflow-hidden bg-canvas">
      {navOpen && (
        <button
          aria-label="Close navigation"
          onClick={() => setNavOpen(false)}
          className="fixed inset-0 z-40 bg-navy-900/40 lg:hidden"
        />
      )}
      <aside
        className={`fixed inset-y-0 left-0 z-50 flex w-[236px] shrink-0 flex-col bg-navy-900 text-white/90 transition-transform lg:static lg:translate-x-0 ${
          navOpen ? "translate-x-0" : "-translate-x-full"
        }`}
      >
        <div className="flex items-center gap-2.5 px-5 py-[18px]">
          <span className="grid h-8 w-8 place-items-center rounded-[7px] bg-white/10 font-mono text-[11px] font-semibold tracking-tight text-white ring-1 ring-white/15">
            RP
          </span>
          <span className="text-[15px] font-semibold tracking-[-0.01em] text-white">
            RevenueProof
          </span>
        </div>

        <nav className="mt-1 px-3">
          {NAV.map(({ href, label, icon: Icon }) => {
            const active =
              href === "/" ? pathname === "/" : pathname.startsWith(href);
            return (
              <Link
                key={href}
                href={href}
                aria-current={active ? "page" : undefined}
                className={`mb-0.5 flex items-center gap-3 rounded-[7px] px-3 py-2 text-[13.5px] transition-colors ${
                  active
                    ? "bg-navy-700 font-medium text-white"
                    : "text-white/65 hover:bg-navy-800 hover:text-white"
                }`}
              >
                <Icon />
                {label}
              </Link>
            );
          })}
        </nav>

        {workspaces.length > 0 && (
          <div className="mt-7 min-h-0 flex-1 overflow-y-auto scroll-thin px-3">
            <div className="px-3 pb-2 text-[10.5px] font-semibold uppercase tracking-[0.09em] text-white/35">
              Workspaces
            </div>
            {workspaces.slice(0, 12).map((ws) => {
              const active = ws.id === activeWorkspaceId;
              return (
                <Link
                  key={ws.id}
                  href={`/workspaces/${ws.id}`}
                  className={`mb-0.5 flex items-center gap-2.5 rounded-[7px] px-3 py-[7px] text-[13px] transition-colors ${
                    active
                      ? "bg-cobalt/20 font-medium text-white ring-1 ring-cobalt/40"
                      : "text-white/60 hover:bg-navy-800 hover:text-white"
                  }`}
                >
                  <span className="grid h-[19px] w-[19px] shrink-0 place-items-center rounded-[5px] bg-white/10 font-mono text-[10px] font-semibold text-white/80">
                    {ws.company_name.slice(0, 1).toUpperCase()}
                  </span>
                  <span className="truncate">{ws.company_name}</span>
                </Link>
              );
            })}
          </div>
        )}

        <div className="mt-auto border-t border-navy-line px-3 py-3">
          <div className="flex items-center gap-2.5 rounded-[7px] px-2 py-1.5">
            <span className="grid h-8 w-8 shrink-0 place-items-center rounded-full bg-cobalt/25 font-mono text-[11px] font-semibold text-white ring-1 ring-white/10">
              {(email ?? "?").slice(0, 2).toUpperCase()}
            </span>
            <span className="min-w-0 flex-1">
              <span className="block truncate text-[12.5px] text-white/85">
                {email ?? "Signed in"}
              </span>
              {onSignOut && (
                <button
                  onClick={onSignOut}
                  className="text-[11.5px] text-white/45 transition-colors hover:text-white/80"
                >
                  Sign out
                </button>
              )}
            </span>
          </div>
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <button
          onClick={() => setNavOpen(true)}
          aria-label="Open navigation"
          className="flex items-center gap-2 border-b border-navy-line bg-navy-900 px-4 py-2.5 text-[13px] text-white/80 lg:hidden"
        >
          <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden>
            <path d="M2.5 4h11M2.5 8h11M2.5 12h11" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
          </svg>
          RevenueProof
        </button>
        {topBar}
        <main className="min-h-0 flex-1 overflow-hidden">{children}</main>
      </div>
    </div>
  );
}

/* Icons — 16px, 1.5 stroke, drawn to sit on the same optical baseline. */

function DashboardIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden>
      <rect x="2" y="2" width="5" height="5" rx="1.2" stroke="currentColor" strokeWidth="1.4" />
      <rect x="9" y="2" width="5" height="5" rx="1.2" stroke="currentColor" strokeWidth="1.4" />
      <rect x="2" y="9" width="5" height="5" rx="1.2" stroke="currentColor" strokeWidth="1.4" />
      <rect x="9" y="9" width="5" height="5" rx="1.2" stroke="currentColor" strokeWidth="1.4" />
    </svg>
  );
}

function FolderIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden>
      <path
        d="M2 4.2c0-.7.5-1.2 1.2-1.2h2.3l1.3 1.6h5c.7 0 1.2.5 1.2 1.2v6c0 .7-.5 1.2-1.2 1.2H3.2c-.7 0-1.2-.5-1.2-1.2V4.2Z"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function ChartIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden>
      <path d="M2.5 13.5V9M6.5 13.5V4M10.5 13.5V7M14 13.5V2.5" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
    </svg>
  );
}

function ListIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden>
      <path d="M5.5 4h8M5.5 8h8M5.5 12h8" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
      <circle cx="2.6" cy="4" r="1" fill="currentColor" />
      <circle cx="2.6" cy="8" r="1" fill="currentColor" />
      <circle cx="2.6" cy="12" r="1" fill="currentColor" />
    </svg>
  );
}
