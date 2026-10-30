import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// /api/* est redirigé vers FastAPI : pas de CORS à configurer.
export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5190,
    strictPort: true,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
        timeout: 300000,
        proxyTimeout: 300000,
      },
    },
  },
});
