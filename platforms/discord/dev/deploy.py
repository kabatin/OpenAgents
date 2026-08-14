#!/usr/bin/env python3
"""Phase 3: 承認されたジョブを live に反映する（👍デプロイ / 👎却下 / 失敗ロールバック）。

worktreeのブランチを main に merge → liveテスト → archivebot再起動 → 復帰確認、の一連。
復帰失敗や テスト赤なら `git reset --keep` で自動ロールバック（未コミットのdirtyな
作業ツリーを壊さないため --hard ではなく --keep を使う）。
merge は worktree のブランチ由来の差分だけを対象にするので、既存のdirtyな別ファイルは触らない。
IO主体。呼び出し順の制御（async）は bot.py 側が持つ。
"""

import os

from platforms.discord.dev.dev_pipeline import (ARCHIVE_REL, REPO_ROOT, _run, remove_worktree,
                          run_tests)

# デプロイ後カナリア監視が見るエラーログ（主戦場=archivebot）
ERR_LOG_PATH = os.path.join(REPO_ROOT, ARCHIVE_REL, "bot.error.log")


def current_head():
    """ロールバック用に main の現在HEADを控える。"""
    return _run(["git", "-C", REPO_ROOT, "rev-parse", "HEAD"]).stdout.strip()


def err_log_size(path=ERR_LOG_PATH):
    """エラーログの現在サイズ（カナリアのベースライン/比較値）。無ければ0。"""
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def is_merge_commit(sha):
    """親が2つ以上あるか（revert方式の選択に使う）。"""
    out = _run(["git", "-C", REPO_ROOT, "rev-list", "--parents", "-n", "1",
                sha]).stdout.split()
    return len(out) > 2


def revert_deploy(pre_sha, post_sha):
    """デプロイ済みの変更を打ち消すコミットを作る（!revert の中核）。

    - post_sha がmergeコミット → `git revert -m 1 post_sha`
    - fast-forwardだった場合 → `git revert pre..post`（範囲を逆順で打ち消し）
    その後のコミットが同じファイルを触っていて競合したら abort して
    (False, 出力) を返す＝壊す前に止めて人間に渡す。"""
    if is_merge_commit(post_sha):
        r = _run(["git", "-C", REPO_ROOT, "revert", "--no-edit",
                  "-m", "1", post_sha])
    else:
        r = _run(["git", "-C", REPO_ROOT, "revert", "--no-edit",
                  f"{pre_sha}..{post_sha}"])
    if r.returncode != 0:
        _run(["git", "-C", REPO_ROOT, "revert", "--abort"])
        return False, (r.stdout + r.stderr)[-400:]
    return True, ""


def commit_worktree(wt, req_id, desc):
    """worktreeの変更を cap-dev ブランチにコミットする。差分無しでも致命ではない。"""
    _run(["git", "-C", wt, "add", "-A"])
    msg = f"feat: 起票#{req_id} {desc[:60]}"
    r = _run(["git", "-C", wt, "commit", "-m", msg])
    return r.returncode == 0 or "nothing to commit" in (r.stdout + r.stderr)


def parse_porcelain(out):
    """`git status --porcelain -z` のNUL区切り出力→ファイル一覧（純粋関数）。
    -z はパスをクォートしない（非ASCIIパスも正確）。R/C エントリの直後の
    レコードは移動元パスなので読み飛ばす。"""
    files, skip = [], False
    for rec in (out or "").split("\0"):
        if skip:                            # 直前が rename/copy → これは移動元
            skip = False
            continue
        if len(rec) < 4:
            continue
        status, path = rec[:2], rec[3:]
        if status and status[0] in ("R", "C"):
            skip = True
        if path:
            files.append(path)
    return files


def dirty_files():
    """mainの未コミット変更（root相対）。liveの手修正が残っているとmergeを阻む。
    -uall で未追跡ディレクトリを畳まず個別ファイルで出す（blocker照合のため）。"""
    return parse_porcelain(_run(["git", "-C", REPO_ROOT, "status",
                                 "--porcelain", "-uall", "-z"]).stdout)


def branch_files(branch):
    """mergeで入る予定の変更ファイル一覧（mainとの共通祖先から見た差分）。"""
    out = _run(["git", "-C", REPO_ROOT, "diff", "--name-only", "-z",
                f"HEAD...{branch}"]).stdout
    return [f for f in out.split("\0") if f.strip()]


def merge_blockers(dirty, incoming):
    """未コミット変更とmerge対象の重なり（純粋関数）。空なら安全にmergeできる。
    起票#7で実証: 重なりがあるとgitがmergeを拒否し👍デプロイが必ず失敗する。"""
    return sorted(set(dirty) & set(incoming))


def merge_branch(branch):
    """cap-dev ブランチを main にmerge。(ok, 出力)。競合/失敗は ok=False。"""
    r = _run(["git", "-C", REPO_ROOT, "merge", "--no-edit", branch])
    if r.returncode != 0:
        _run(["git", "-C", REPO_ROOT, "merge", "--abort"])   # 競合は必ず中断して現状維持
    return r.returncode == 0, (r.stdout + r.stderr)[-500:]


def merged_files(pre_sha):
    """mergeで入った変更ファイル一覧（テスト/再起動対象の導出に使う）。"""
    out = _run(["git", "-C", REPO_ROOT, "diff", "--name-only",
                f"{pre_sha}..HEAD"]).stdout
    return [f for f in out.splitlines() if f.strip()]


def run_live_tests(files=None):
    """反映後の live で回帰テスト。変更ファイルから対象スイートを導出して回す。"""
    ok, tail = run_tests(REPO_ROOT, files)
    return ok, tail[-600:]


def rollback(pre_sha, expect_head=None):
    """merge を取り消して pre_sha に戻す。--keep で未コミットの変更は保持。
    expect_head 指定時、現HEADが一致しない（＝その間に人間のコミットが入った）
    場合は巻き戻さない（他人のコミットを道連れにしない）。"""
    if expect_head:
        head = current_head()
        if head != expect_head:
            return False, f"HEADが想定({expect_head[:8]})と不一致({head[:8]})のため自動rollback中止"
    r = _run(["git", "-C", REPO_ROOT, "reset", "--keep", pre_sha])
    return r.returncode == 0, (r.stdout + r.stderr)[-300:]


def cleanup(wt, branch):
    """成功/却下後に worktree とブランチを掃除する。"""
    remove_worktree(wt, branch)
