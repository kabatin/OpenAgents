import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// フロントは web/ 配下。ビルド成果物は dist/ に出し、本番は Hono が静的配信する。
// 開発時は Vite(5173) → API(8787) へプロキシ。どちらも 127.0.0.1 のみ。
export default defineConfig({
  root: "web",
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8787",
        changeOrigin: false,
        // SSE をバッファさせない
        configure: (proxy) => {
          proxy.on("proxyRes", (proxyRes) => {
            proxyRes.headers["cache-control"] = "no-cache";
          });
        },
      },
    },
  },
  build: {
    outDir: "../dist",
    emptyOutDir: true,
  },
});
