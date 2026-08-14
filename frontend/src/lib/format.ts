/**
 * Display formatting. No arithmetic happens here or anywhere else in the frontend —
 * every figure shown is one the backend computed and can cite.
 */

/**
 * Indian digit grouping: three, then twos. `1,50,00,000`, not `15,000,000`.
 *
 * Typed into a bare input a crore is nine unbroken characters, and nobody can tell
 * 1,50,00,000 from 15,00,00,000 at a glance — least of all the person entering the
 * figure their whole report will be measured against.
 */
export function groupIndian(digits: string): string {
  if (digits.length <= 3) return digits;
  const head = digits.slice(0, -3);
  const tail = digits.slice(-3);
  const parts: string[] = [];
  let rest = head;
  while (rest.length > 2) {
    parts.unshift(rest.slice(-2));
    rest = rest.slice(0, -2);
  }
  if (rest) parts.unshift(rest);
  return `${parts.join(",")},${tail}`;
}

/** Display value for a money input, keeping at most two decimal places. */
export function formatAmountInput(raw: string): string {
  const cleaned = raw.replace(/[^\d.]/g, "");
  const [whole = "", ...rest] = cleaned.split(".");
  const fraction = rest.join("").slice(0, 2);
  const grouped = groupIndian(whole.replace(/^0+(?=\d)/, ""));
  return cleaned.includes(".") ? `${grouped}.${fraction}` : grouped;
}

/** Strip the grouping before it goes to the API, which wants a plain decimal. */
export function plainAmount(display: string): string {
  return display.replace(/,/g, "") || "0";
}

/** Minor units to a grouped display string, for currencies the backend didn't format. */
export function fromMinor(minor: number | null | undefined, currency = "INR"): string {
  if (minor === null || minor === undefined) return "—";
  const whole = Math.trunc(Math.abs(minor) / 100);
  const paise = String(Math.abs(minor) % 100).padStart(2, "0");
  const digits =
    currency === "INR"
      ? groupIndian(String(whole))
      : String(whole).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return `${minor < 0 ? "−" : ""}${currency} ${digits}.${paise}`;
}

/** Seconds to a compact duration a reviewer can scan: 4s, 1:22, 12:04. */
export function duration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 60) return `${seconds < 10 ? seconds.toFixed(1) : Math.round(seconds)}s`;
  const mins = Math.floor(seconds / 60);
  const secs = Math.round(seconds % 60);
  return `${mins}:${String(secs).padStart(2, "0")}`;
}

/** A clock for the running-time badge. */
export function elapsedClock(ms: number): string {
  const total = Math.floor(ms / 1000);
  return `${String(Math.floor(total / 60)).padStart(2, "0")}:${String(total % 60).padStart(2, "0")}`;
}

export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "—";
  const secs = Math.round((Date.now() - then) / 1000);
  if (secs < 45) return "just now";
  if (secs < 3600) return `${Math.round(secs / 60)} min ago`;
  if (secs < 86400) return `${Math.round(secs / 3600)} h ago`;
  return new Date(iso).toLocaleDateString(undefined, {
    day: "numeric",
    month: "short",
    year: "numeric",
  });
}
