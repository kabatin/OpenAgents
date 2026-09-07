"""stream-json のツール結果から -# 行（証拠）を決定論で作る（純粋関数）。

- write ツールの結果は evidence をそのまま並べる（成功🟢/失敗⚠️は結果由来）
- 失敗した write が1件でもあれば呼び出し側が失敗を1行目に置く
  （最終結果が失敗なら1行目で失敗を伝える、という誠実さの原則）
- read は原則出さない。search_messages のヒット0だけ根拠として1行出す
- permission_denials は RBAC の穴の検出器として警告行にする"""

import json

from core.archive_tools import registry
from core.archive_tools.launch import SERVER_NAME

registry.ensure_loaded()

PREFIX = f"mcp__{SERVER_NAME}__"


def _short(name):
    return name[len(PREFIX):] if name.startswith(PREFIX) else None


def _result_text(content):
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text") or "")
    return "\n".join(parts)


def tool_results(events):
    """archive ツールの呼び出しと結果を出現順に並べる。
    [{"name", "input", "ok", "parsed"(dict|None), "is_error"}]"""
    calls = {}   # tool_use_id -> (name, input)
    out = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        content = (ev.get("message") or {}).get("content") or []
        if ev.get("type") == "assistant":
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    calls[b.get("id")] = (b.get("name") or "",
                                          b.get("input") or {})
        elif ev.get("type") == "user":
            for b in content:
                if not (isinstance(b, dict)
                        and b.get("type") == "tool_result"):
                    continue
                name, inp = calls.get(b.get("tool_use_id"), ("", {}))
                short = _short(name)
                if short is None:
                    continue
                parsed = None
                try:
                    parsed = json.loads(_result_text(b.get("content")))
                except ValueError:
                    parsed = None
                if not isinstance(parsed, dict):
                    parsed = None
                ok = bool(parsed.get("ok")) if parsed else False
                out.append({"name": short, "input": inp, "ok": ok,
                            "parsed": parsed,
                            "is_error": bool(b.get("is_error"))})
    return out


def tools_used(events):
    """使った archive ツール名（短い名前・出現順・重複なし）。"""
    seen = []
    for r in tool_results(events):
        if r["name"] not in seen:
            seen.append(r["name"])
    return seen


def denied_tools(events):
    for ev in events:
        if isinstance(ev, dict) and ev.get("type") == "result":
            return [d.get("tool_name") or "?"
                    for d in ev.get("permission_denials") or []]
    return []


def build_notes(events):
    """-# 行のリストと、失敗した write ツール名のリストを返す。"""
    notes = []
    failed = []
    writes = registry.write_tool_names()
    searched_empty = False
    for r in tool_results(events):
        if r["name"] in writes:
            parsed = r["parsed"] or {}
            ev_line = parsed.get("evidence")
            if r["ok"]:
                notes.append(ev_line or f"-# ✅ {r['name']} を実行")
            else:
                failed.append(r["name"])
                err = parsed.get("error") or "理由不明"
                notes.append(ev_line or f"-# ⚠️ {r['name']} に失敗: {err}")
        elif (r["name"] == "search_messages" and r["ok"]
              and (r["parsed"] or {}).get("hits") == 0):
            searched_empty = True
    if searched_empty:
        notes.append("-# 🔎 社内ログを検索したが該当なし")
    for d in denied_tools(events):
        notes.append(f"-# ⚠️ 権限外の操作を試みました（{d}）")
    return notes, failed


def summarize_for_review(events, max_items=8):
    """自己採点（self_review）に渡す根拠の要約（純粋関数）。
    使ったツール・引数・ヒット数・結果の冒頭を1行ずつ。無ければ空文字。"""
    lines = []
    for r in tool_results(events)[:max_items]:
        parsed = r["parsed"] or {}
        arg = json.dumps(r["input"], ensure_ascii=False)[:80]
        if r["ok"]:
            hits = parsed.get("hits")
            head = (parsed.get("evidence") or parsed.get("message") or "")
            head = head.replace("\n", " ")[:120]
            hit_s = f" hits={hits}" if hits is not None else ""
            lines.append(f"- {r['name']}({arg}) → ok{hit_s}: {head}")
        else:
            err = (parsed.get("error") or "失敗")[:80]
            lines.append(f"- {r['name']}({arg}) → 失敗: {err}")
    return "\n".join(lines)


def dedupe_notes(answer, notes):
    """本文に既に同じ行があるものは除く（モデルがツール結果の evidence を本文に
    写した場合の二重表示を防ぐ）。"""
    body_lines = {ln.strip() for ln in (answer or "").splitlines()}
    out = []
    for n in notes:
        keep = [ln for ln in n.splitlines() if ln.strip() not in body_lines]
        if keep:
            out.append("\n".join(keep))
    return out


def search_hits(events):
    """archive の検索・台帳ツールが返したヒット数の合計（honesty の「根拠なし断定」判定に
    事前注入のヒット数と合算して使う。注入を減らすとヒット0が常態になるため）。"""
    total = 0
    for r in tool_results(events):
        if r["ok"] and r["name"] in ("search_messages", "get_facts",
                                     "get_decisions"):
            h = (r["parsed"] or {}).get("hits")
            total += int(h) if isinstance(h, int) else 0
    return total
