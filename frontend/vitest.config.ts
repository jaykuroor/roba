import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// Test runner config (02 §B9). jsdom gives the panels a DOM to render into;
// the setup file wires @testing-library/jest-dom matchers; globals lets the
// specs use describe/it/expect without imports.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
