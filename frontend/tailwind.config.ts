import type { Config } from "tailwindcss";

// Design tokens (00 §23): a restrained palette, Inter type, and a fixed
// control-bar height. All classes used in the UI are statically named so they
// are present in the compiled stylesheet (no dynamic class generation).
const config: Config = {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        primary: "#1a1a2e",
        accent: "#e94560",
        surface: "#16213e",
        muted: "#0f3460",
        text: "#eaeaea",
        success: "#4caf50",
        warning: "#ff9800",
        danger: "#f44336",
      },
      fontFamily: {
        sans: ["Inter", "ui-sans-serif", "system-ui", "sans-serif"],
      },
      spacing: {
        18: "4.5rem",
      },
    },
  },
  plugins: [],
};

export default config;
