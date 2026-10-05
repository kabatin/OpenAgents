/**
 * サブループ（proactive_state の "<機能コード>:<対象>" 行）を人の言葉にする。
 *
 * 機能コード（svdistill 等）のままでは何のことか分からないので、名前・1行説明・
 * 実行周期を持たせる。周期は「止まっているか」の判定に使う（月次の機能は
 * 3週間前が最終実行でも正常。一律の日数で判定すると誤って止まって見える）。
 * 機能コードは core/ と platforms/discord/ の STATE_PREFIX 等と揃える。
 */

export type LoopInfo = {
  label: string;
  desc: string;
  everyDays: number;
  /** 廃止・停止した機能（最終実行が新しくても「動いていない」側に畳む） */
  retired?: boolean;
};

export const LOOP_INFO: Record<string, LoopInfo> = {
  minutes: { label: "議事録の読み取り", desc: "議事録から期日つきのTODOを拾う", everyDays: 1 },
  homework: { label: "宿題の拾い上げ", desc: "「やっときます」を黙って記録する", everyDays: 1 },
  attention: { label: "会話の見守り", desc: "チャンネルごとの読み進めた位置", everyDays: 1 },
  briefing: { label: "朝のブリーフィング", desc: "朝に今日の予定と気になる点をまとめる", everyDays: 1 },
  comebackscan: { label: "不在明けの検知", desc: "しばらく来ていない人の把握", everyDays: 1 },
  comeback: { label: "不在明けのまとめ", desc: "戻ってきた人に不在中のあらすじを渡す", everyDays: 1 },
  audit: { label: "その日の自己点検", desc: "夜にその日の発言を振り返る", everyDays: 1 },
  ripple: { label: "決定の波及チェック", desc: "新しい決定とぶつかる古い決定・ずれる期日やリマインダーを探す", everyDays: 7 },
  ruledistill: { label: "ルールの棚卸し", desc: "重複・古くなったルールの整理を提案（週1）", everyDays: 7 },
  svdistill: { label: "自己採点のまとめ", desc: "低評価の回答から改善メモを作る（週1）", everyDays: 7 },
  report: { label: "週次の自発レポート", desc: "金曜に1週間の自発行動を報告", everyDays: 7 },
  devreport: { label: "開発BOTの週次レポート", desc: "金曜に開発の実績を報告", everyDays: 7 },
  prep: { label: "定例の事前パック", desc: "定例会議の前に未完了タスクと決定をまとめる", everyDays: 7 },
  outreach: { label: "御用聞き", desc: "困っていそうな人に手伝えることを聞く", everyDays: 7 },
  newspaper: { label: "社内新聞", desc: "週1回の社内の出来事まとめ", everyDays: 7 },
  eventwatch: { label: "イベントの節目ウォッチ", desc: "行事の節目を見張る", everyDays: 7 },
  news: { label: "業界ニュースの巡回", desc: "関係しそうなニュースを拾う", everyDays: 7 },
  capwatch: { label: "起票の拾い上げ", desc: "新しい起票を開発BOTの提案に回す", everyDays: 30 },
  pulse: { label: "満足度アンケート", desc: "月1回の1問アンケート", everyDays: 31 },
  personareview: { label: "人格チューニングの提案", desc: "月1回の人格の見直し", everyDays: 31 },
  autodiscover: { label: "自動化ネタの発掘", desc: "繰り返し作業を探して起票を提案（月1）", everyDays: 31 },
  demand: { label: "欠員の発見", desc: "新しい担当が必要そうか調べる（月1）", everyDays: 31 },
  drill: { label: "乗っ取り耐性の訓練", desc: "月1回の自己テスト（突破時だけ報告）", everyDays: 31 },
  kpi: { label: "四半期の目標宣言", desc: "四半期ごとの目標と振り返り", everyDays: 92 },
  study: { label: "勉強会", desc: "学びをほかのエージェントに横展開する", everyDays: 31 },
  bias: { label: "偏りの自己開示", desc: "自分の偏りを点検して公開する", everyDays: 31 },
  checkup: { label: "人格と実態のズレ点検", desc: "人格定義と実際の発言を比べる", everyDays: 31 },
  orgchart: { label: "組織図の掲示", desc: "エージェントの担当表を更新する", everyDays: 31 },
  abreport: { label: "話し方のA/B実験の報告", desc: "実験の結果を報告する", everyDays: 31 },
};

export function loopInfo(code: string): LoopInfo {
  return LOOP_INFO[code] ?? { label: code, desc: "（説明未登録の機能コード）", everyDays: 7 };
}

/** DBの素のJST文字列からの経過日数。読めなければ null。 */
export function ageDays(raw: string | null, nowMs = Date.now()): number | null {
  if (raw === null) return null;
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(raw);
  if (!m) return null;
  const [, y, mo, d, h, mi] = m.map(Number) as number[];
  const jstMs = Date.UTC(y!, mo! - 1, d!, h!, mi!) - 9 * 3600 * 1000;
  return (nowMs - jstMs) / 86_400_000;
}

/** 廃止・停止した機能か、周期の1.5倍を過ぎても動いていなければ「止まっている」。 */
export function isStale(code: string, lastRunAt: string | null, nowMs = Date.now()): boolean {
  if (loopInfo(code).retired === true) return true;
  const age = ageDays(lastRunAt, nowMs);
  return age === null || age > loopInfo(code).everyDays * 1.5;
}
