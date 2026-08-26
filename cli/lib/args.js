/**
 * 引数の解釈。副作用を持たない純粋関数にしてある（テストの本体）。
 */

/** 受け付けるサブコマンド。ここに無いものは打ち間違いとして扱う */
export const COMMANDS = [
  "setup",
  "start",
  "stop",
  "status",
  "update",
  "where",
  "help",
  "version",
];

/**
 * argv（実行ファイル2つを除いた残り）を解釈する。
 *
 * 戻り値: { command, dir, restart, help, version, unknown }
 * `unknown` に値が入っていたら、呼び出し側は打ち間違いとして扱うこと。
 */
export function parseArgs(argv) {
  const result = {
    command: null,
    dir: null,
    restart: false,
    help: false,
    version: false,
    unknown: null,
  };

  for (let i = 0; i < argv.length; i += 1) {
    const token = argv[i];

    if (token === "--help" || token === "-h") {
      result.help = true;
      continue;
    }
    if (token === "--version" || token === "-v" || token === "-V") {
      result.version = true;
      continue;
    }
    if (token === "--restart") {
      result.restart = true;
      continue;
    }
    if (token === "--dir") {
      // 値が続かない `--dir` は指定漏れ。黙って既定に落とすと
      // 「指定したのに別の場所が使われた」という最悪の事故になる
      const value = argv[i + 1];
      if (value === undefined || value.startsWith("-")) {
        result.unknown = "--dir（置き場のパスが指定されていません）";
        return result;
      }
      result.dir = value;
      i += 1;
      continue;
    }
    if (token.startsWith("--dir=")) {
      const value = token.slice("--dir=".length);
      if (!value) {
        result.unknown = "--dir=（置き場のパスが空です）";
        return result;
      }
      result.dir = value;
      continue;
    }
    if (token.startsWith("-")) {
      result.unknown = token;
      return result;
    }
    if (result.command === null) {
      if (!COMMANDS.includes(token)) {
        result.unknown = token;
        return result;
      }
      result.command = token;
      continue;
    }
    result.unknown = token;
    return result;
  }

  // 無引数は setup。初めて打つ人が最短で前に進める側に倒す
  if (result.command === null && !result.help && !result.version) {
    result.command = "setup";
  }
  return result;
}

export const HELP = `
OpenAgents — チャットに住みつくAIエージェント

  openagents setup            入れて、設定画面をブラウザで開く（無引数のときはこれ）
  openagents start            常駐プロセスを前面で動かす（Ctrl-C で止まる）
  openagents stop             動いている常駐プロセスを止める
  openagents status           いま動いているかを見る
  openagents update           最新に更新する（--restart で常駐も入れ替える）
  openagents where            置き場のパスを出す

オプション

  --dir <path>    置き場を指定する（環境変数 OPENAGENTS_HOME でも指定できます）
  --restart       update のあと、動いている常駐プロセスを入れ替える
  --version       バージョンを出す
  --help          この説明

置き場は次の順に決まります:
  1. --dir            2. OPENAGENTS_HOME
  3. いま居る場所が OpenAgents の中なら、そこ
  4. ~/.openagents

くわしくは https://github.com/kabatin/OpenAgents/blob/main/docs/11-cli.md
`.trim();
