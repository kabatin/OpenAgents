/**
 * 配色は既存の週次レポートグラフ（discord-archive/dashboard.py の COLORS）から取っている。
 * Discordに流れるグラフと画面の色が揃うので、同じ物を見ている感覚になる。
 * ダークモードは意図的に持たない。
 */
export default {
  content: ["./web/**/*.{ts,tsx,html}", "./index.html"],
  theme: {
    extend: {
      colors: {
        canvas: "#FAFAF8",
        surface: "#FFFFFF",
        hairline: "#E8E6E1",
        ink: "#17181A",
        muted: "#77746D",
        faint: "#A8A5A0",
        accent: {
          DEFAULT: "#0E7A68", // spoke（自発発言）の緑
          soft: "#E6F2EF",
          deep: "#0A5A4D",
        },
        warn: { DEFAULT: "#C98A2B", soft: "#FBF0DE" }, // nudge（納期の声かけ）
        info: { DEFAULT: "#3A6EA5", soft: "#E8EFF7" }, // decisions（決定事項）
        plum: { DEFAULT: "#7A5EA5", soft: "#F0EBF7" }, // golden（ゴールデン）
        danger: { DEFAULT: "#B3352B", soft: "#FAEAE8" },
      },
      fontFamily: {
        sans: [
          "-apple-system",
          "BlinkMacSystemFont",
          "Hiragino Sans",
          "Hiragino Kaku Gothic ProN",
          "Noto Sans JP",
          "sans-serif",
        ],
        mono: ["ui-monospace", "SFMono-Regular", "SF Mono", "Menlo", "monospace"],
      },
      fontSize: {
        "2xs": ["0.6875rem", { lineHeight: "1rem" }],
      },
      boxShadow: {
        card: "0 1px 2px rgba(23, 24, 26, 0.04)",
        pop: "0 8px 28px -6px rgba(23, 24, 26, 0.16)",
      },
    },
  },
  plugins: [],
};
