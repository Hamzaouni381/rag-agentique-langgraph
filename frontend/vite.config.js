import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// En dev, /api est redirige vers FastAPI (port 8001) : pas de CORS a gerer.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/api": { target: "http://localhost:8001", changeOrigin: true } },
  },
});
