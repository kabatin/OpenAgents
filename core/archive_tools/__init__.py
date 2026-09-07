"""archive-tools: エージェントが回答の途中で社内データを引く／書くための
MCP サーバ（v4 Phase 1）。設計: docs/08-architecture.md「ツールループ」

構成:
  context.py     実行文脈（誰が・どのchで・どのスキルで・dry-runか）
  registry.py    ツール定義と dispatch（純粋・RBAC はここ）
  tools_read.py  読み取りツール（既存モジュールへの薄い層）
  tools_write.py 書き込みツール（権限判定と -# 書式は marker_actions から移設）
  server.py      stdio JSON-RPC（claude -p が起動する側）
  launch.py      bot 側: --mcp-config 文字列と allow ルールの組み立て
  evidence.py    stream-json のツール結果から -# 行を決定論で作る
依存は増やさない（手書き JSON-RPC・標準ライブラリのみ）。"""
