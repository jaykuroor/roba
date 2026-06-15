import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The browser always hits the frontend origin; Vite proxies API + WebSocket
// to the backend service so there are no CORS/origin issues (00 §26.4).
// All frontend calls use relative paths (/api/..., /ws) — never a hardcoded host.
export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    port: 5173,
    proxy: {
      "/api": {
        target: process.env.BACKEND_ORIGIN ?? "http://localhost:8000",
        changeOrigin: true,
      },
      "/ws": {
        target: process.env.BACKEND_ORIGIN ?? "http://localhost:8000",
        ws: true,
      },
    },
  },
});
