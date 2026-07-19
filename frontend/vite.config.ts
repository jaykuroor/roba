import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import basicSsl from "@vitejs/plugin-basic-ssl";

// The browser always hits the frontend origin; Vite proxies API + WebSocket
// to the backend service so there are no CORS/origin issues (00 §26.4).
// All frontend calls use relative paths (/api/..., /ws) — never a hardcoded host.
// basicSsl() enables HTTPS so navigator.mediaDevices is available on LAN devices.
export default defineConfig({
  plugins: [react(), basicSsl()],
  server: {
    host: true,   // expose on all interfaces so other LAN devices can connect
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
      // Manager server (multi-restaurant): /admin/api = dashboard endpoints,
      // /i/<instance>/... = per-instance HTTP + WS proxy (docs/fable).
      "/admin/api": {
        target: process.env.MANAGER_ORIGIN ?? "http://localhost:8100",
        changeOrigin: true,
      },
      "/i": {
        target: process.env.MANAGER_ORIGIN ?? "http://localhost:8100",
        changeOrigin: true,
        ws: true,
      },
    },
  },
});
