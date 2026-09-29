/**
 * 自発ループの分類（catalog.proactive.ts の `category`）。
 * 30を超えるサイクルを1列に並べると初めての人には選べないので、目的ごとの小見出しにまとめる。
 * 並び順＝画面の表示順（よく触るものを上に）。
 */
export const CYCLE_CATEGORIES: { id: string; label: string; desc: string }[] = [
  { id: "meetings", label: "会議とタスク", desc: "議事録・宿題・会議の準備を追いかける" },
  { id: "conversation", label: "会話の見守り", desc: "放置された質問や、決定と食い違う記録、止まった話題に気づく" },
  { id: "reports", label: "報告と発信", desc: "週次レポート・朝のブリーフィング・アンケートなど" },
  { id: "learning", label: "記憶と学習", desc: "投稿はせず、人物像・出来事・ルールを整理して覚える" },
  { id: "info", label: "情報収集", desc: "イベントの節目や業界ニュースを見回る" },
  { id: "selfcheck", label: "自己点検と実験", desc: "自分についての定期的な点検や、話し方の実験" },
];
