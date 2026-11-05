import { AlertTriangle, Search } from "lucide-react";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { useDeadlineRisk, useMe, useWorkQueues } from "@/api/queries";
import { deadlineWorkPath } from "@/api/routes";
import { isTypingContext, setShortcutsEnabled, useShortcutsEnabled } from "@/lib/preferences";
import { signOut } from "@/lib/session";

function initials(subject: string): string {
  const parts = subject.split(/[^A-Za-z]+/).filter(Boolean);
  return ((parts[0]?.[0] ?? "") + (parts[1]?.[0] ?? "")).toUpperCase() || "?";
}

const NAV_LINK = "text-md text-on-nav-muted hover:text-on-nav focus-visible:outline-focus-on-nav";

export function TopBar() {
  const me = useMe();
  const risk = useDeadlineRisk();
  const queues = useWorkQueues();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const shortcuts = useShortcutsEnabled();
  const search = useRef<HTMLInputElement>(null);
  // UR-22: the search lives in the URL, so a reload or a shared link keeps it.
  const [query, setQuery] = useState(() => params.get("q") ?? "");

  useEffect(() => {
    if (!shortcuts) return;
    function onKey(event: KeyboardEvent) {
      if (event.key === "/" && !isTypingContext(event)) {
        event.preventDefault();
        search.current?.focus();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
    };
  }, [shortcuts]);

  function onSearch(event: FormEvent) {
    event.preventDefault();
    const q = query.trim();
    if (q) navigate(`/search?q=${encodeURIComponent(q)}`);
  }

  const alerts = risk.data?.claims ?? 0;
  const subject = me.data?.subject;

  return (
    <header className="flex h-[var(--topbar-height)] shrink-0 items-center gap-6 bg-nav px-6 text-on-nav">
      <Link to="/" className="text-lg font-bold tracking-tight focus-visible:outline-focus-on-nav">
        RCM Platform
      </Link>

      <form
        role="search"
        aria-label="Claims"
        onSubmit={onSearch}
        className="w-[var(--search-width)] max-w-full"
      >
        <label className="flex h-7 items-center gap-2 rounded-control bg-nav-raised px-3 text-md focus-within:outline-2 focus-within:outline-focus-on-nav">
          <Search aria-hidden="true" className="size-4 text-on-nav-muted" />
          <span className="sr-only">Search claims</span>
          <input
            ref={search}
            value={query}
            onChange={(event) => {
              setQuery(event.target.value);
            }}
            placeholder="Search claim ID or payer"
            className="min-w-0 flex-1 bg-transparent text-on-nav placeholder:text-on-nav-muted focus:outline-none"
          />
          {shortcuts && (
            <kbd className="text-sm text-on-nav-muted" aria-hidden="true">
              /
            </kbd>
          )}
        </label>
      </form>

      <div className="ml-auto flex items-center gap-6">
        {alerts > 0 && (
          <Link
            to={deadlineWorkPath(queues.data?.queues)}
            className="flex items-center gap-2 text-md focus-visible:outline-focus-on-nav"
          >
            <span
              aria-hidden="true"
              className="tabular flex h-5 min-w-5 items-center justify-center gap-1 rounded-full bg-danger px-1 text-sm font-bold"
            >
              <AlertTriangle className="size-3" />
              {alerts}
            </span>
            <span>
              Alerts<span className="sr-only">: {alerts} claims at filing-deadline risk</span>
            </span>
          </Link>
        )}
        <button
          type="button"
          aria-pressed={shortcuts}
          onClick={() => {
            setShortcutsEnabled(!shortcuts);
          }}
          className={`cursor-pointer ${NAV_LINK}`}
        >
          Shortcuts {shortcuts ? "on" : "off"}
        </button>
        {subject && (
          <span
            title={subject}
            className="flex size-7 items-center justify-center rounded-full bg-accent text-sm font-bold"
          >
            <span aria-hidden="true">{initials(subject)}</span>
            <span className="sr-only">Signed in as {subject}</span>
          </span>
        )}
        {/* Always reachable: a slow or failing /me must never trap an operator. */}
        <button
          type="button"
          onClick={() => {
            signOut();
          }}
          className={`cursor-pointer ${NAV_LINK}`}
        >
          Sign out
        </button>
      </div>
    </header>
  );
}
