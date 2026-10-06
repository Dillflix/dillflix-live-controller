import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { resolve } from "node:path";
export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      input: { admin: resolve("index.html"), public: resolve("public.html") },
    },
  },
  server: { proxy: { "/api": { target: "http://127.0.0.1:8790", ws: true } } },
});
