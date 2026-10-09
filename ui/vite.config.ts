import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import pkg from "./package.json" with { type: "json" };

const BACKEND = "http://localhost:8400";

// Dev server is pinned to 5180 (strictPort: fail rather than drift to another
// port). /api, /mcp, /healthz and /metrics are proxied to the FastAPI backend.
export default defineConfig(({ mode }) => ({
  plugins: [react()],
  // D6: the Docker build passes VITE_APP_VERSION (from pyproject.toml); a local build
  // falls back to package.json, kept equal to pyproject.toml by a lockstep test.
  define: {
    "import.meta.env.VITE_APP_VERSION": JSON.stringify(loadEnv(mode, ".", "VITE_").VITE_APP_VERSION || pkg.version),
  },
  server: {
    port: 5180,
    strictPort: true,
    proxy: {
      "/api": BACKEND,
      "/mcp": BACKEND,
      "/healthz": BACKEND,
      "/metrics": BACKEND,
    },
  },
  preview: { port: 5180, strictPort: true },
  build: {
    // Fluent v9 is one ~720 kB barrel chunk (~200 kB gzip); acceptable for a
    // localhost admin tool. The enforced per-chunk budgets live in
    // scripts/check-bundle.mjs (`npm run check:bundle` after a build); this
    // warning sits at the fluent budget so a regression past it also warns here.
    chunkSizeWarningLimit: 737, // kB (= the 720 KiB fluent budget)
    rollupOptions: {
      output: {
        manualChunks: {
          react: ["react", "react-dom", "react-router"],
          fluent: ["@fluentui/react-components"],
        },
      },
    },
  },
}));
