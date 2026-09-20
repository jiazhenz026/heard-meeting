import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// base "./" so dist/ can be served from any path the runtime mounts it at.
export default defineConfig({
  base: "./",
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/ws": {
        target: "http://127.0.0.1:8000",
        ws: true,
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
  },
});
