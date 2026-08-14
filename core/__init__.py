"""プラットフォーム非依存の中核。

ここには Discord も Slack も出てこない。会話プラットフォームとのやりとりは
`core.chat` が定めるインターフェース越しに行い、実装は `platforms/` 側に置く。

**このパッケージから discord 等のSDKを import してはいけない**
（CIの test_layering がそれを検査している）。
"""
