// Global test setup: registers @testing-library/jest-dom matchers (toBeInTheDocument,
// etc.) and auto-cleans the DOM between tests so panels render in isolation.
import "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

afterEach(() => {
  cleanup();
});
