import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterEach, beforeAll, vi } from "vitest";

// Each test starts from an empty page, no session and the real fetch.
afterEach(() => {
  cleanup();
  window.sessionStorage.clear();
  vi.unstubAllGlobals();
});

// The screens are lazy-loaded (App.tsx). Compiling one, with everything it
// imports, can take longer than findBy's one second on a slow machine, so
// whichever test in a file rendered it first failed there and nowhere else.
// Load them before any test starts waiting; the compile is cached after.
const screens = import.meta.glob("../screens/*Screen.tsx");
beforeAll(async () => {
  await Promise.all(Object.values(screens).map((load) => load()));
});
