import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { apiGet, instancePagePrefix } from "../api";
import type { MenuItem, SimState, MenuToggleEvent } from "../types";
import { wsClient } from "../ws";

// Public customer menu. Opens the shared WebSocket so it receives `menu_toggled`
// events instantly when an ingredient is zeroed or a station goes unstaffed.
// Also polls GET /api/menu at a slow cadence as a safety net for brief WS outages.
// Shows every item — active ones are orderable, inactive ones are greyed out as
// "Sold out".

const POLL_MS = 60_000;

function prettifySeedId(id: string | null | undefined): string {
  if (!id) return "Our Menu";
  return id
    .split(/[_-]/)
    .filter(Boolean)
    .map((w) => w[0].toUpperCase() + w.slice(1))
    .join(" ");
}

function fmtPrice(n: number | null): string {
  if (n === null || n === undefined) return "";
  return `$${n.toFixed(2)}`;
}

export default function MenuPage() {
  const [items, setItems] = useState<MenuItem[]>([]);
  const [title, setTitle] = useState("Our Menu");
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    apiGet<SimState & { active_seed_id?: string }>("/api/sim/state")
      .then((s) => setTitle(prettifySeedId(s.active_seed_id)))
      .catch(() => undefined);

    let cancelled = false;
    const load = () => {
      apiGet<MenuItem[]>("/api/menu")
        .then((rows) => {
          if (cancelled) return;
          setItems(rows);
          setLoaded(true);
        })
        .catch(() => undefined);
    };
    load();
    const timer = setInterval(load, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, []);

  useEffect(() => {
    // /menu is outside OperatorLayout so no other code opens the socket here.
    // connect() is idempotent and re-opens a socket closed by OperatorLayout on nav.
    wsClient.connect();
    const off = wsClient.on("menu_toggled", (p) => {
      const { menu_item_id, action } = p as unknown as MenuToggleEvent;
      setItems((prev) =>
        prev.map((item) =>
          item.id === menu_item_id
            ? { ...item, active: action === "enable" ? 1 : 0 }
            : item,
        ),
      );
    });
    return off;
  }, []);

  const grouped = useMemo(() => {
    const byCategory = new Map<string, MenuItem[]>();
    for (const item of items) {
      const cat = item.category ?? "Other";
      const list = byCategory.get(cat) ?? [];
      list.push(item);
      byCategory.set(cat, list);
    }
    return [...byCategory.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  }, [items]);

  return (
    <div className="min-h-screen bg-stone-50 text-stone-900">
      <header className="border-b border-stone-200 bg-white">
        <div className="mx-auto max-w-3xl px-6 py-6">
          <Link
            to={instancePagePrefix() || "/admin"}
            className="mb-4 inline-flex items-center gap-1 text-xs text-stone-400 hover:text-stone-600"
          >
            ← Operator console
          </Link>
          <h1 className="text-3xl font-semibold tracking-tight">{title}</h1>
          <p className="mt-1 text-sm text-stone-500">Menu</p>
        </div>
      </header>

      <main className="mx-auto max-w-3xl px-6 py-8">
        {!loaded ? (
          <div className="py-16 text-center text-stone-400">Loading menu…</div>
        ) : items.length === 0 ? (
          <div className="py-16 text-center text-stone-400">
            No menu loaded. Seed a restaurant from the operator console.
          </div>
        ) : (
          grouped.map(([category, catItems]) => (
            <section key={category} className="mb-10">
              <h2 className="mb-4 border-b border-stone-200 pb-1 text-xs font-semibold uppercase tracking-widest text-stone-400">
                {category}
              </h2>
              <ul className="space-y-4">
                {catItems.map((item) => {
                  const available = item.active === 1;
                  return (
                    <li
                      key={item.id}
                      className={
                        available
                          ? "flex items-baseline justify-between gap-4"
                          : "flex items-baseline justify-between gap-4 opacity-50"
                      }
                    >
                      <div>
                        <div className="flex items-center gap-2">
                          <span
                            className={
                              available
                                ? "text-base font-medium"
                                : "text-base font-medium line-through"
                            }
                          >
                            {item.name}
                          </span>
                          {!available ? (
                            <span className="rounded bg-stone-200 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-stone-500">
                              Sold out
                            </span>
                          ) : null}
                        </div>
                        {item.description ? (
                          <p className="mt-0.5 text-sm text-stone-500">
                            {item.description}
                          </p>
                        ) : null}
                      </div>
                      <div className="shrink-0 text-base tabular-nums text-stone-700">
                        {fmtPrice(item.dine_in_price)}
                      </div>
                    </li>
                  );
                })}
              </ul>
            </section>
          ))
        )}
      </main>
    </div>
  );
}
