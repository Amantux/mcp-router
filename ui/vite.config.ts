import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const BACKEND = "http://localhost:8400";

// Dev server is pinned to 5180 (strictPort: fail rather than drift to another
// port). /api, /mcp, /healthz and /metrics are proxied to the FastAPI backend.
export default defineConfig({
  plugins: [react()],
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
});
