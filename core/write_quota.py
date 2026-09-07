#!/usr/bin/env python3
"""書き込み枠（1ユーザー1日の回数上限）の DB 版。

プロセス内カウンタは MCP サーバ（1回答1プロセス）からは毎回ゼロから数えてしまい
上限が効かない。`allow(user_id, today)` の形で DB（write_quota テーブル）に数える。
同一プロセス内の複数スレッドからも安全。外部連携（integrations）が自分の書き込み枠を
持ちたいときも scope を分けてこれを使える。"""

from core import db


class DbWriteQuota:
    def __init__(self, db_path, scope, limit):
        self.db_path = db_path
        self.scope = scope
        self.limit = int(limit)

    def allow(self, user_id, today):
        with db.connect(self.db_path) as conn:
            return db.write_quota_allow(conn, self.scope, str(user_id),
                                        today, self.limit)
