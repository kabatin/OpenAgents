#!/usr/bin/env python3
"""開発BOT（つむぎ）の声 — 元気で明るい女の子らしい後輩エンジニア。

監視通知・進捗・コマンド応答などの定型文はここで一元管理（純粋関数・テスト対象）。
メンションへの自由会話は claude -p にペルソナ(personas/devbot.md)を渡して生成する
（bot.py 側。これは定型ではないためLLMに任せる）。

口調の規約: 明るい敬語ベース（〜です！／〜しますね〜／やりますっ！）。
「〜っす」系の男性的な言葉遣いは使わない（2026-07-31 管理者指示）。
"""

from platforms.discord.dev import monitor

NAME = "開発BOT"
EMOJI = "🧵"


def startup(names, interval_sec):
    return (f"🧵 監視スタートです！{names} のこと、"
            f"{interval_sec}秒ごとにしっかり見張っておきますね〜✨")


def alert(name, status):
    """状態遷移の通知（つむぎの一言）。"""
    if status == monitor.DOWN:
        return f"⚠️ 大変です！**{name}** が落ちてます！？プロセス止まってる…見てきますね🔧"
    if status == monitor.DISCONNECTED:
        return (f"🟡 あれ？**{name}** がDiscordから切れちゃってるみたいです。"
                f"プロセスは生きてるんですけど…👀")
    if status == monitor.STALLED:
        return f"🟠 **{name}**、しばらく反応がないです…念のため見てもらえると安心かもです"
    if status == monitor.OK:
        return f"✅ **{name}** 復活です〜！よかった〜、もう大丈夫です🎉"
    return f"{name}: {status}"


def status_header():
    return "🧵 いまの様子はこんな感じです↓"


def ping():
    return "ぽんっ！🧵 元気に動いてますよ〜"


def job_start(req_id, desc):
    return (f"🧵 起票#{req_id}、あたしがやりますっ！ちょっと待っててくださいね〜\n"
            f"> {desc[:120]}")


def progress(phase, ops, last):
    tail = f"／さっき: {last}" if last else ""
    return f"🔧 {phase}です…（{ops}手目{tail}）"


def job_phase_test():
    return "🧪 テストとpyflakes回してます…もうちょっとです！"


def job_finished(req_id, ok):
    if ok:
        return f"🧵 起票#{req_id}、実装できました！中身のチェックお願いします〜（承認待ち）"
    return f"🧵 起票#{req_id}、いちおう出しました…テスト赤なので確認してほしいです💦"


def job_error(req_id, err):
    return f"うぅ、起票#{req_id} の実装でコケちゃいました…ごめんなさい🙇（{err}）"


def resuming(req_id):
    return (f"🧵 起票#{req_id}、前回の途中経過が残っていたので**続きから**再開します！"
            "（ゼロからやり直したい時は『起票 #N 作り直し』って言ってくださいね）")


def job_interrupted(req_id):
    return (f"🧵 あれ、起票#{req_id} の実装が途中で止まってました…（たぶん再起動で中断です）"
            f"ごめんなさい🙇 もう一回『起票 #{req_id} やって』で再挑戦できます！")


# --- Phase 3: 承認ゲート ---
def rejected(req_id):
    return f"🧵 起票#{req_id}、却下ですね〜。作ったものは片付けておきました！また必要になったら言ってください🔧"


def ask_reject_reason(req_id):
    return (f"もしよければ、起票#{req_id} のダメだった点を**このメッセージへの返信**で"
            "一言もらえると、次から気をつけます🙏（スルーでも大丈夫です）")


def lesson_saved():
    return "教訓メモしました！次に活かしますね📝"


def approving(req_id):
    return (f"🧵 起票#{req_id}、承認ありがとうございます！liveに反映して再起動しますね…"
            f"ちょっと待っててください🔧")


def restarting(name="archivebot"):
    return f"🔧 {name} 再起動中です…戻ってくるまで見てますね👀"


def self_restarting():
    return ("🧵 あたし自身の改修が入ったので、いったん再起動して反映します！"
            "30秒くらいで戻ってきますね〜👋")


def deployed(req_id):
    return f"🎉 起票#{req_id}、live反映＆再起動できました！みんな無事に戻ってきましたよ🧵✨"


def deploy_failed(req_id, why):
    return f"うぅ、起票#{req_id} のデプロイに失敗しちゃいました…（{why}）ごめんなさい🙇"


def deploy_blocked_dirty(req_id, names):
    shown = "、".join(names[:5]) + ("…" if len(names) > 5 else "")
    return (f"⚠️ 起票#{req_id}、mainに未コミットの手修正が残っていて、同じファイル"
            f"（{shown}）を触るのでmergeできないです。先にコミットしてもらえたら、"
            "もう一回👍で反映しますね🙏")


def deploy_rolled_back(req_id, why="対象BOTが復帰せず"):
    return (f"⚠️ 起票#{req_id}、{why}だったので**自動で元に戻しました**"
            f"（ロールバック）。原因を調べた方がよさそうです💦")


# --- 進化ロードマップ（カード運用） ---
def roadmap_started(rm_id, cap_id):
    return (f"🗺️ #{rm_id} 承認ありがとうございます！起票#{cap_id}にして、"
            "さっそく実装始めますね🔧")


def roadmap_queued(rm_id, cap_id):
    return (f"🗺️ #{rm_id} 承認です！起票#{cap_id}にしました。いま別の実装中なので、"
            f"手が空いたら『起票 #{cap_id} やって』で呼んでほしいです🙏")


def roadmap_session(rm_id):
    return (f"🗺️ #{rm_id} 承認です！これは大物なので**管理者セッション行き**に"
            "積んでおきました📌（一覧は `!roadmap`）")


def roadmap_skipped(rm_id):
    return f"🗺️ #{rm_id} は見送りですね〜。次の案いきます！"


def roadmap_all_done():
    return "🗺️ ロードマップのご提案、ぜんぶ出し切りました！おつかれさまでした🎉"


# --- 起票の自動拾い上げ（RM#21） ---
def cap_accepted(cap_id):
    return f"🧵 起票#{cap_id}、あたしが引き受けます！さっそく取りかかりますね🔧"


def cap_queued(cap_id):
    return (f"🧵 起票#{cap_id}、了解です！いま別の実装中なので、"
            f"手が空いたら『起票 #{cap_id} やって』で呼んでほしいです🙏")


def cap_declined(cap_id):
    return f"🧵 起票#{cap_id} は見送りですね。台帳は閉じておきますね！"


# --- !revert（デプロイ後の巻き戻し） ---
def reverting(cap_id):
    return f"🔧 起票#{cap_id} のデプロイを巻き戻します…ちょっと待っててくださいね"


def reverted(cap_id, tests_ok):
    base = f"↩️ 起票#{cap_id} の変更を打ち消して再起動しました！"
    if tests_ok:
        return base + "テストも緑です✅"
    return base + "⚠️ ただしテストに赤があるので、一度見てほしいです"


def revert_failed(cap_id, why):
    return (f"うぅ、起票#{cap_id} の巻き戻しに失敗しました…（{why}）"
            "後から入った変更と競合しているかもなので、手動確認お願いします🙇")


def revert_not_found(cap_id):
    return (f"起票#{cap_id} のデプロイ記録が見つからないです…"
            "（!revert できるのは記録がある分だけなんです）")


def canary_alert(cap_id, grown_kb):
    return (f"⚠️ デプロイ後の見張りで気づきました: 起票#{cap_id} の反映後、"
            f"エラーログが{grown_kb}KB増えてます。様子が変だったら "
            f"`!revert {cap_id}` で巻き戻せますよ🔧")


def not_admin():
    return "すみません先輩、開発の指示は管理者の方だけなんです🙏"


def job_not_found(req_id):
    return f"あれ？起票#{req_id} が見つからないです…番号あってますか？"


def already_running(req_id):
    return f"起票#{req_id} はいま実装中です！終わるまでちょっと待っててくださいね〜"
