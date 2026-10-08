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
  build: {
    // Fluent v9 is one ~640 kB barrel chunk (~185 kB gzip); acceptable for a
    // localhost admin tool. Raised so a real regression still warns.
    chunkSizeWarningLimit: 700,
    rollupOptions: {
      output: {
        manualChunks: {
          react: ["react", "react-dom", "react-router"],
          fluent: ["@fluentui/react-components"],
        },
      },
    },
  },
});
