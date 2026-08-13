"use client";

/**
 * Dependency and credential banner.
 *
 * Surfacing this in the UI is a deliberate honesty mechanism: Step 2a's
 * integration reality-check asks whether a call actually went out. A reviewer can
 * see at a glance which sources hold live credentials and which are running on the
 * synthetic dataset, so a demo can never be mistaken for a real reconciliation.
 */

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { HealthStatus } from "@/lib/types";

function Dot({ ok }: { ok: boolean }) {
  return (
    <span
      className={`inline-block h-2 w-2 rounded-full ${ok ? "bg-emerald-500" : "bg-slate-300"}`}
      aria-hidden
    />
  );
}

export function ServiceStatus() {
  const [health, setHealth] = useState<HealthStatus | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    const load = () =>
      api
        .health()
        .then((h) => active && (setHealth(h), setError(null)))
        .catch((e) => active && setError(e.message));
    load();
    const timer = setInterval(load, 15000);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, []);

  if (error) {
    return (
      <div className="rounded-md bg-rose-50 px-3 py-2 text-xs text-rose-700">
        Backend unreachable: {error}
      </div>
    );
  }
  if (!health) return null;

  const services = [
    { name: "PostgreSQL", ok: health.services.postgres.ok },
    { name: "Redis", ok: health.services.redis.ok },
    { name: "Neo4j", ok: health.services.neo4j.ok },
    { name: "LLM", ok: health.services.llm.ok },
  ];
  const liveProviders = Object.entries(health.providers).filter(([, ok]) => ok);

  return (
    <div className="flex flex-wrap items-center gap-x-5 gap-y-2 rounded-md border border-slate-200 bg-white px-3 py-2 text-xs">
      {services.map((service) => (
        <span key={service.name} className="flex items-center gap-1.5 text-slate-600">
          <Dot ok={service.ok} />
          {service.name}
        </span>
      ))}
      <span className="text-slate-400">|</span>
      <span className="text-slate-600">
        Live credentials:{" "}
        {liveProviders.length > 0 ? (
          <span className="font-medium text-emerald-700">
            {liveProviders.map(([name]) => name).join(", ")}
          </span>
        ) : (
          <span className="font-medium text-amber-700">
            none — connectors will use the synthetic dataset
          </span>
        )}
      </span>
    </div>
  );
}
