/** "Copy State" button — gathers current sim state and writes a plain-text
 *  report to the clipboard. Placed inside the Advanced section. */
import { useState } from "react";
import { ClipboardCopy } from "lucide-react";
import { gatherState, buildReport } from "./copyState";

export function CopyStateButton() {
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleCopy() {
    setBusy(true);
    setError(null);
    try {
      const bundle = await gatherState();
      const report = buildReport(bundle);
      await navigator.clipboard.writeText(report);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Copy failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-1">
      <button
        type="button"
        onClick={() => void handleCopy()}
        disabled={busy}
        className="flex items-center gap-2 rounded-md bg-accent px-3 py-2 text-sm font-medium text-white hover:bg-accent/80 disabled:opacity-50"
      >
        <ClipboardCopy size={14} />
        {copied ? "✓ Copied" : busy ? "Gathering…" : "Copy State"}
      </button>
      {error && <p className="text-xs text-danger">{error}</p>}
    </div>
  );
}
