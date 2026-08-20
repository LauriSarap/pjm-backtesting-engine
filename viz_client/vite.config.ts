import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwind from "@tailwindcss/vite";

// Vite dev server. /api/* proxies to the FastAPI viz_server. Localhost only.
export default defineConfig({
  plugins: [react(), tailwind()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": {
        target: process.env.VIZ_SERVER_URL ?? "http://127.0.0.1:8765",
        changeOrigin: false,
      },
    },
  },
  build: {
    target: "esnext",
    sourcemap: true,
  },
});
