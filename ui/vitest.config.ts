import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: false,
    // The first test in a file also pays for module import + JIT; on a contended CI
    // runner that alone has exceeded the 5 s default (seen under a 1-core repro).
    testTimeout: 20000,
  },
});
