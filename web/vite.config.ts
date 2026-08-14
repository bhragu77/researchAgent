import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The built bundle is served by the API itself (StaticFiles), so relative asset
// paths keep it working behind a tunnel or a sub-path without rebuilding.
export default defineConfig({
  plugins: [react()],
  base: "./",
  build: { outDir: "dist", emptyOutDir: true },
  server: {
    port: 5173,
    // Dev-only: talk to the local API without CORS.
    proxy: { "/v1": { target: "http://127.0.0.1:8123", changeOrigin: true } },
  },
});
