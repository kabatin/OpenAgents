import { defineConfig } from "vitest/config";

// vite.config.ts は root を web/ にしているため、テストは別設定にする
// （フロントのビルド設定とテストの探索範囲を混ぜない）。
export default defineConfig({
  test: {
    root: ".",
    include: ["tests/**/*.test.ts"],
    environment: "node",
    globals: true,
  },
});
