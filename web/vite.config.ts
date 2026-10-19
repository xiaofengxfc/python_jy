import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The Python backend owns every /api and /ws route. In development Vite serves
// the UI only; in a release build the bundle is written into the backend's
// static directory and served from there.
const BACKEND = process.env.BITRADER_BACKEND ?? "http://127.0.0.1:8765";

export default defineConfig({
  // The backend serves the bundle from /static (see web/app.py), so asset URLs
  // must carry that prefix; the root path stays the app's page route.
  base: "/static/",
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: false,
    proxy: {
      "/api": { target: BACKEND, changeOrigin: false },
      "/fonts": { target: BACKEND, changeOrigin: false },
      "/ws": { target: BACKEND.replace("http", "ws"), ws: true },
    },
  },
  build: {
    outDir: "../src/btrader/web/static",
    emptyOutDir: true,
    sourcemap: false,
  },
});
