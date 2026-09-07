"""ツール実行の文脈。bot 側で組み立て、サーバにはコマンドライン引数で渡す。
LLM の申告は一切信用せず、権限判定は全部この値から行う。"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ToolContext:
    agent_id: str
    actor_id: str                 # 発言者（人間）の Discord user id
    db_path: str
    guild_id: str
    channel_id: int | None = None
    message_id: int | None = None
    bot_turn: bool = False        # Bot 起点ターン: write 系を見せない
    dry_run: bool = False         # シャドー: write 系を見せない（read のみ）
    is_admin: bool = False
    skills: frozenset = field(default_factory=frozenset)
    reminder_max_active: int | None = None   # エージェント設定（上限）
    question: str = ""            # 発言本文（起票の context 用・500字まで）
    agent_ids: tuple = ()         # 実在するエージェントid（枠変更の検証用）

    def to_args(self):
        """server.py に渡すコマンドライン引数（build と parse は対）。"""
        args = ["--agent", self.agent_id, "--actor", self.actor_id,
                "--db", self.db_path, "--guild", self.guild_id,
                "--skills", ",".join(sorted(self.skills))]
        if self.channel_id is not None:
            args += ["--channel", str(self.channel_id)]
        if self.message_id is not None:
            args += ["--message", str(self.message_id)]
        if self.bot_turn:
            args.append("--bot-turn")
        if self.dry_run:
            args.append("--dry-run")
        if self.is_admin:
            args.append("--admin")
        if self.reminder_max_active:
            args += ["--reminder-max", str(int(self.reminder_max_active))]
        if self.question:
            args += ["--question", self.question[:500]]
        if self.agent_ids:
            args += ["--agents", ",".join(self.agent_ids)]
        return args

    @classmethod
    def from_args(cls, argv):
        import argparse
        p = argparse.ArgumentParser(prog="archive-tools")
        p.add_argument("--agent", required=True)
        p.add_argument("--actor", required=True)
        p.add_argument("--db", required=True)
        p.add_argument("--guild", required=True)
        p.add_argument("--skills", default="")
        p.add_argument("--channel", type=int, default=None)
        p.add_argument("--message", type=int, default=None)
        p.add_argument("--bot-turn", action="store_true")
        p.add_argument("--dry-run", action="store_true")
        p.add_argument("--admin", action="store_true")
        p.add_argument("--reminder-max", type=int, default=None)
        p.add_argument("--question", default="")
        p.add_argument("--agents", default="")
        a = p.parse_args(argv)
        skills = frozenset(s for s in a.skills.split(",") if s)
        return cls(agent_id=a.agent, actor_id=a.actor, db_path=a.db,
                   guild_id=a.guild, channel_id=a.channel,
                   message_id=a.message, bot_turn=a.bot_turn,
                   dry_run=a.dry_run, is_admin=a.admin, skills=skills,
                   reminder_max_active=a.reminder_max,
                   question=a.question or "",
                   agent_ids=tuple(x for x in a.agents.split(",") if x))
