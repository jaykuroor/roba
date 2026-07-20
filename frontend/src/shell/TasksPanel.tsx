import { useEffect, useState } from "react";
import { apiGet } from "../api";

// Kitchen checklist board for the manager: what was done, done late, overdue
// or reported not done today. Read-only mirror of the cook desk's task list
// (/api/kitchen/tasks/board) — outcomes are recorded by the kitchen; overdue
// escalation notices land in the Approval Inbox (docs/fable/approvals.md).

interface TaskRow {
  id: number;
  title: string;
  category: string;
  station: string | null;
  due_sim_time: number | null;
  details: string[];
  status: "pending" | "done" | "not_done" | "skipped";
  note: string | null;
  overdue: boolean;
  overdue_min: number;
  late: boolean;
  late_min: number;
  done_by: string | null;
}

interface TaskBoard {
  sim_day: number;
  counts: {
    done: number;
    done_late: number;
    pending: number;
    overdue: number;
    not_done: number;
    skipped: number;
  };
  tasks: TaskRow[];
}

const POLL_MS = 5000;

function dueLabel(dueSimTime: number | null): string {
  if (dueSimTime == null) return "—";
  const rest = dueSimTime % 86400;
  const hh = String(Math.floor(rest / 3600)).padStart(2, "0");
  const mm = String(Math.floor((rest % 3600) / 60)).padStart(2, "0");
  return `${hh}:${mm}`;
}

function StatusChip({ task }: { task: TaskRow }) {
  if (task.status === "done" && task.late) {
    return (
      <span className="rounded bg-amber-500/20 px-2 py-0.5 text-xs font-medium text-amber-400">
        done late +{task.late_min}m
      </span>
    );
  }
  if (task.status === "done") {
    return (
      <span className="rounded bg-success/20 px-2 py-0.5 text-xs font-medium text-success">
        done
      </span>
    );
  }
  if (task.status === "not_done") {
    return (
      <span className="rounded bg-danger/20 px-2 py-0.5 text-xs font-medium text-danger">
        not done
      </span>
    );
  }
  if (task.overdue) {
    return (
      <span className="rounded bg-danger/20 px-2 py-0.5 text-xs font-medium text-danger">
        overdue {task.overdue_min}m
      </span>
    );
  }
  return (
    <span className="rounded bg-muted px-2 py-0.5 text-xs font-medium text-text/50">
      pending
    </span>
  );
}

function CountChip({ label, value, cls }: { label: string; value: number; cls: string }) {
  return (
    <span className={`rounded px-2 py-0.5 text-xs font-medium ${cls}`}>
      {value} {label}
    </span>
  );
}

export function TasksPanel() {
  const [board, setBoard] = useState<TaskBoard | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const load = () => {
      apiGet<TaskBoard>("/api/kitchen/tasks/board")
        .then((b) => {
          if (!cancelled) {
            setBoard(b);
            setError(false);
          }
        })
        .catch(() => {
          if (!cancelled) setError(true);
        });
    };
    load();
    const timer = setInterval(load, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, []);

  if (board == null) {
    return (
      <div className="p-4 text-sm text-text/40" data-panel="Tasks">
        {error ? "Task board unavailable." : "Loading tasks…"}
      </div>
    );
  }

  const { counts } = board;
  return (
    <div
      className="flex h-full flex-col gap-3 overflow-auto rounded-lg bg-surface/40 p-3"
      data-panel="Tasks"
    >
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-sm font-semibold text-text">Kitchen Tasks — Day {board.sim_day}</h2>
        <div className="ml-auto flex flex-wrap gap-1.5">
          <CountChip label="done" value={counts.done} cls="bg-success/20 text-success" />
          {counts.done_late > 0 && (
            <CountChip label="late" value={counts.done_late} cls="bg-amber-500/20 text-amber-400" />
          )}
          {counts.overdue > 0 && (
            <CountChip label="overdue" value={counts.overdue} cls="bg-danger/20 text-danger" />
          )}
          {counts.not_done > 0 && (
            <CountChip label="not done" value={counts.not_done} cls="bg-danger/20 text-danger" />
          )}
          <CountChip label="pending" value={counts.pending} cls="bg-muted text-text/50" />
        </div>
      </div>

      <table className="w-full text-left text-sm">
        <thead>
          <tr className="text-xs uppercase tracking-wide text-text/40">
            <th className="py-1 pr-3 font-medium">Due</th>
            <th className="py-1 pr-3 font-medium">Task</th>
            <th className="py-1 pr-3 font-medium">Category</th>
            <th className="py-1 pr-3 font-medium">Status</th>
            <th className="py-1 font-medium">Note</th>
          </tr>
        </thead>
        <tbody>
          {board.tasks.map((task) => (
            <tr key={task.id} className="border-t border-muted/40">
              <td className="py-1.5 pr-3 tabular-nums text-text/60">
                {dueLabel(task.due_sim_time)}
              </td>
              <td className="py-1.5 pr-3 text-text">
                {task.title}
                {task.station && (
                  <span className="ml-1.5 text-xs text-text/40">({task.station})</span>
                )}
              </td>
              <td className="py-1.5 pr-3 text-xs uppercase text-text/40">{task.category}</td>
              <td className="py-1.5 pr-3">
                <StatusChip task={task} />
              </td>
              <td className="py-1.5 text-xs text-text/50">{task.note ?? ""}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
