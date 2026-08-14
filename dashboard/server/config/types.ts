/**
 * 設定カタログの型。
 *
 * 画面は手書きせず、この型で宣言されたデータを描画するだけにする。
 * BOT側に新しい機能が生えたら catalog/ に1行足せば画面に出る
 * （既存の agent_loops.py `_cycle_plan()` と同じ思想）。
 */

export type SettingKind =
  | "bool" // ON/OFF
  | "tri" // OFF / シャドー / 本番（{enabled, shadow} の組を1つのUIで扱う）
  | "int"
  | "string"
  | "text" // 複数行
  | "enum"
  | "stringList"
  | "intList"
  | "hour" // 0-23
  | "weekday" // 0=月 〜 6=日
  | "monthday" // 1-31
  | "info"; // 表示のみ（編集不可の事実）

export type EnumOption = { value: string | number | null; label: string };

/** 「この設定を有効にするには別の設定もONが要る」という依存の明示 */
export type Requirement = { path: string; label: string };

export type Setting = {
  /** 所属スコープのルートからの相対パス。例: 'proactive.rescue.shadow' */
  path: string;
  label: string;
  /** 非エンジニアに1文で伝わる説明 */
  desc: string;
  kind: SettingKind;
  /** キーが無いときにコード側が使う実際の既定値 */
  default?: unknown;
  min?: number;
  max?: number;
  unit?: string;
  options?: EnumOption[];
  /** 画面から変えさせない（壊すと起動しなくなる / 秘密情報） */
  readonly?: boolean;
  /**
   * 「親オブジェクトの存在＝有効」な設定（image_gen 等）の .enabled リーフ用。
   * リーフが未設定でも親がオブジェクトとして存在すれば ON と解決する。
   * ※bool トグルは必ずリーフを指すこと。オブジェクトのノードを bool で
   *   指すと、トグルがオブジェクトを true/false で上書きして設定を破壊する
   *   （実際に起きた事故。tests/config.test.ts が検査している）
   */
  presenceIsOn?: boolean;
  secret?: boolean;
  requires?: Requirement[];
  /** 「実行時刻はコード固定」など、設定できない事実の注記 */
  fixedNote?: string;
  /** 行を展開したときに出す詳細パラメータ */
  children?: Setting[];
};

export type SettingGroup = {
  id: string;
  label: string;
  desc?: string;
  settings: Setting[];
};

/** 曜日の選択肢（Pythonの weekday() に合わせて 0=月） */
export const WEEKDAY_OPTIONS: EnumOption[] = [
  { value: 0, label: "月曜" },
  { value: 1, label: "火曜" },
  { value: 2, label: "水曜" },
  { value: 3, label: "木曜" },
  { value: 4, label: "金曜" },
  { value: 5, label: "土曜" },
  { value: 6, label: "日曜" },
];

/**
 * 3値トグルのヘルパ。
 * OFF → {enabled:false} / シャドー → {enabled:true, shadow:true} / 本番 → {enabled:true, shadow:false}
 */
export type TriValue = "off" | "shadow" | "live";

export function triFrom(obj: unknown, shadowDefault: boolean): TriValue {
  const o = (obj ?? {}) as Record<string, unknown>;
  if (!o["enabled"]) return "off";
  const shadow = o["shadow"] === undefined ? shadowDefault : Boolean(o["shadow"]);
  return shadow ? "shadow" : "live";
}

export function triTo(value: TriValue): { enabled: boolean; shadow: boolean } {
  if (value === "off") return { enabled: false, shadow: true };
  return { enabled: true, shadow: value === "shadow" };
}
