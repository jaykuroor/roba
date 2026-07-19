// Deterministic per-restaurant identity mark (docs/fable/manager-dashboard.md):
// a colored tile with the preset's emoji when known, otherwise the title's
// initials. The color is hashed from the instance id, so the same restaurant
// looks the same on the admin grid, the instance nav, and anywhere else —
// with zero stored assets.

export const PRESET_EMOJI: Record<string, string> = {
  bellas_kitchen: "🍝",
  burger_joint: "🍔",
};

const PALETTE = [
  "#e94560", "#7c5cbf", "#2e86ab", "#0f8b6c",
  "#c77d1e", "#b23a68", "#3d6b99", "#7a8b2e",
];

export function logoColor(id: string): string {
  let h = 0;
  for (const ch of id) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return PALETTE[h % PALETTE.length];
}

export function RestaurantLogo({
  id,
  title,
  preset,
  size = 40,
}: {
  id: string;
  title?: string;
  preset?: string | null;
  size?: number;
}) {
  const emoji = preset ? PRESET_EMOJI[preset] : undefined;
  const initials = (title || id)
    .split(/[\s_]+/)
    .map((w) => w[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();
  return (
    <div
      style={{ width: size, height: size, backgroundColor: logoColor(id) }}
      className="flex shrink-0 select-none items-center justify-center rounded-lg font-semibold text-white"
      title={title || id}
      aria-hidden
    >
      {emoji ? (
        <span style={{ fontSize: size * 0.55, lineHeight: 1 }}>{emoji}</span>
      ) : (
        <span style={{ fontSize: size * 0.38, lineHeight: 1 }}>{initials}</span>
      )}
    </div>
  );
}
