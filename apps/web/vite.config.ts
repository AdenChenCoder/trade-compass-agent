/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";

const WEB_DIST = path.resolve(__dirname, "../../src/trade_compass_agent/web_dist");

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": path.resolve(__dirname, "./src") },
  },
  test: {
    environment: "jsdom",
    globals: false,
  },
  server: {
    host: "127.0.0.1",
    port: 3000,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:19704",
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: WEB_DIST,
    emptyOutDir: true,
    rollupOptions: {
      output: {
        manualChunks: {
          "vendor-react": ["react", "react-dom", "react-router-dom"],
          "vendor-charts": ["echarts"],
          "vendor-markdown": ["react-markdown", "remark-gfm"],
          "vendor-query": ["@tanstack/react-query"],
        },
      },
    },
  },
});
