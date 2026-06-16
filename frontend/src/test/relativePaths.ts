// Spec guard (02 §B9, 00 §26.4): the frontend must talk to the backend only via
// relative paths so the Vite proxy routes them — never a hardcoded host. Given a
// mocked api helper (apiGet/apiPatch/apiPost), assert every path argument it was
// called with is relative.
import { expect } from "vitest";
import type { Mock } from "vitest";

const ABSOLUTE_URL = /^[a-z]+:\/\//i;

export function isRelativePath(path: string): boolean {
  if (ABSOLUTE_URL.test(path)) return false; // http://, https://, ws://, ...
  if (path.startsWith("//")) return false; // protocol-relative still escapes origin
  return path.startsWith("/") || path.startsWith("./");
}

/** Assert that the first argument of every recorded call is a relative path. */
export function assertRelativePaths(...mocks: Mock[]): void {
  let asserted = false;
  for (const mock of mocks) {
    for (const call of mock.mock.calls) {
      const path = call[0];
      expect(typeof path).toBe("string");
      expect(isRelativePath(path as string), `path "${path}" must be relative`).toBe(true);
      asserted = true;
    }
  }
  // Make sure the panel actually called the api layer at least once.
  expect(asserted).toBe(true);
}
