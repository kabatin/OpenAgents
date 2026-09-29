#!/usr/bin/env python3
"""README の図（SVG）を作る。日本語版と英語版を同じ配置から出す。

Mermaid は GitHub 上で文字が小さく、配色もテーマ任せで読みにくかったので、
ヒーロー画像（docs/images/hero.svg）と同じ見た目の SVG を手で組む。
文字を画像生成に任せないのは、日本語が崩れず、あとから文言を直せるようにするため。

使い方: python docs/images/diagrams/build.py   （docs/images/ に *.svg を書き出す）
"""

import os
from xml.sax.saxutils import escape

OUT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
FONT = ("-apple-system, 'Hiragino Sans', 'Hiragino Kaku Gothic ProN', "
        "'Noto Sans JP', 'Yu Gothic UI', 'Segoe UI', 'Helvetica Neue', Arial, sans-serif")
MONO = "'SF Mono', Menlo, Consolas, 'Noto Sans Mono', monospace"

INK, MUTED, FAINT = "#E8ECF8", "#A7B0CC", "#6F7A99"
CARD, CARD_LINE = "#141C33", "#2A3558"
CYAN, INDIGO, AMBER = "#4CC9E8", "#7C8CFF", "#F2B84B"

DEFS = f"""
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#0B1020"/><stop offset="60%" stop-color="#101A33"/>
      <stop offset="100%" stop-color="#0A0F1E"/>
    </linearGradient>
    <linearGradient id="accent" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0%" stop-color="{INDIGO}"/><stop offset="100%" stop-color="{CYAN}"/>
    </linearGradient>
    <linearGradient id="accentFill" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="{INDIGO}" stop-opacity="0.28"/>
      <stop offset="100%" stop-color="{CYAN}" stop-opacity="0.22"/>
    </linearGradient>
    <pattern id="dots" width="26" height="26" patternUnits="userSpaceOnUse">
      <circle cx="1.5" cy="1.5" r="1.5" fill="#8FA0D0" fill-opacity="0.08"/>
    </pattern>
    <marker id="arrow" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
      <path d="M0,0 L10,5 L0,10 z" fill="{MUTED}"/>
    </marker>
    <marker id="arrowAccent" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
      <path d="M0,0 L10,5 L0,10 z" fill="{CYAN}"/>
    </marker>
  </defs>"""


def svg(w, h, title, body):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
            f'viewBox="0 0 {w} {h}" role="img" aria-label="{escape(title)}">\n'
            f"  <title>{escape(title)}</title>{DEFS}\n"
            f'  <rect width="{w}" height="{h}" rx="20" fill="url(#bg)"/>\n'
            f'  <rect width="{w}" height="{h}" rx="20" fill="url(#dots)"/>\n'
            f"{body}\n</svg>\n")


def text(x, y, s, *, size=16, color=INK, weight=400, anchor="middle", font=FONT,
         spacing=None, lh=1.45):
    """複数行は \\n で区切る（行間は size*1.45）。y は1行目のベースライン。"""
    lines = s.split("\n")
    ls = f' letter-spacing="{spacing}"' if spacing else ""
    out = [f'  <text x="{x}" y="{y}" font-family="{font}" font-size="{size}" '
           f'font-weight="{weight}" fill="{color}" text-anchor="{anchor}"{ls}>']
    for i, line in enumerate(lines):
        dy = 0 if i == 0 else size * lh
        out.append(f'<tspan x="{x}" dy="{dy:.1f}">{escape(line)}</tspan>')
    out.append("</text>")
    return "".join(out)


def box(x, y, w, h, *, fill=CARD, stroke=CARD_LINE, dash=None, rx=14, width=1.5):
    d = f' stroke-dasharray="{dash}"' if dash else ""
    return (f'  <rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="{width}"{d}/>')


def card(x, y, w, h, title, sub=None, *, mono=False, accent=False, dash=None,
         title_size=19, sub_size=14):
    """見出し＋説明の2段カード。中央揃え。"""
    parts = [box(x, y, w, h,
                 fill="url(#accentFill)" if accent else CARD,
                 stroke="url(#accent)" if accent else CARD_LINE,
                 dash=dash, width=2 if accent else 1.5)]
    n_title = len(title.split("\n"))
    n_sub = len(sub.split("\n")) if sub else 0
    title_h = title_size + (n_title - 1) * title_size * 1.3
    block = title_h + (n_sub * sub_size * 1.45 + 8 if sub else 0)
    ty = y + (h - block) / 2 + title_size * 0.82
    parts.append(text(x + w / 2, ty, title, size=title_size, weight=700,
                      font=MONO if mono else FONT, lh=1.3))
    if sub:
        parts.append(text(x + w / 2, ty + title_h - title_size * 0.5 + sub_size + 6, sub,
                          size=sub_size, color=MUTED))
    return "\n".join(parts)


def arrow(d, *, label=None, lx=0, ly=0, accent=False, dash=None, anchor="middle"):
    col = CYAN if accent else MUTED
    m = "arrowAccent" if accent else "arrow"
    ds = f' stroke-dasharray="{dash}"' if dash else ""
    out = [f'  <path d="{d}" fill="none" stroke="{col}" stroke-width="2"{ds} '
           f'marker-end="url(#{m})"/>']
    if label:
        out.append(text(lx, ly, label, size=14, color=col, weight=600, anchor=anchor))
    return "\n".join(out)


def eyebrow(x, y, s):
    return text(x, y, s, size=13, color=CYAN, weight=700, anchor="start", spacing="0.06em")


# ---------------------------------------------------------------- 関門の図

GATE = {
    "ja": dict(
        title="自分から話す前に、どのループも同じ関門を通る",
        loop=("観察ループ", "30種類が\n数分おきに見回る"),
        q1="言うべきことが\nあるか？", q2="シャドー\nモードか？",
        q3="今日の上限内？\n深夜ではない？",
        post=("Discordに投稿", None),
        rec=("記録だけして、投稿しない", "管理画面で中身を読み、発言させるかを人が決める"),
        silent="ない → 黙る", yes="ある", no="いいえ", ok="はい",
        shadow_yes="はい", over="いいえ"),
    "en": dict(
        title="Every loop passes the same gate before it speaks",
        loop=("Observation loops", "30 of them scan\nevery few minutes"),
        q1="Anything actually\nworth saying?", q2="In shadow\nmode?",
        q3="Under today's cap?\nOutside quiet hours?",
        post=("Post to\nDiscord", None),
        rec=("Recorded, never posted", "You read it in the dashboard and decide whether it may talk"),
        silent="no → stay quiet", yes="yes", no="no", ok="yes",
        shadow_yes="yes", over="no"),
}


def gate(lang):
    t = GATE[lang]
    W, H = 1280, 470
    y, h = 150, 104
    L = (40, y, 220, h)
    Q1, Q2, Q3 = (320, y, 210, h), (590, y, 190, h), (840, y, 220, h)
    P = (1112, y, 136, h)
    b = [eyebrow(40, 58, t["title"].upper() if lang == "en" else t["title"])]
    b.append(card(*L, t["loop"][0], t["loop"][1]))
    for q, s in ((Q1, t["q1"]), (Q2, t["q2"]), (Q3, t["q3"])):
        b.append(box(*q, rx=h / 2, stroke=INDIGO, width=1.8))
        n = len(s.split("\n"))
        b.append(text(q[0] + q[2] / 2, q[1] + h / 2 - (n - 1) * 17 * 1.45 / 2 + 6, s,
                      size=17, weight=600))
    b.append(card(*P, t["post"][0], None, accent=True, title_size=17))
    cy = y + h / 2
    # 本線
    b.append(arrow(f"M{L[0]+L[2]} {cy} H{Q1[0]-4}", accent=True))
    b.append(arrow(f"M{Q1[0]+Q1[2]} {cy} H{Q2[0]-4}", accent=True,
                   label=t["yes"], lx=(Q1[0] + Q1[2] + Q2[0]) / 2, ly=cy - 12))
    b.append(arrow(f"M{Q2[0]+Q2[2]} {cy} H{Q3[0]-4}", accent=True,
                   label=t["no"], lx=(Q2[0] + Q2[2] + Q3[0]) / 2, ly=cy - 12))
    b.append(arrow(f"M{Q3[0]+Q3[2]} {cy} H{P[0]-4}", accent=True,
                   label=t["ok"], lx=(Q3[0] + Q3[2] + P[0]) / 2, ly=cy - 12))
    # 言うことが無い → 黙って次の見回りへ
    x1, x2 = Q1[0] + Q1[2] / 2, L[0] + L[2] / 2
    b.append(arrow(f"M{x1} {y} C{x1} {y-62} {x2} {y-62} {x2} {y-4}",
                   label=t["silent"], lx=(x1 + x2) / 2, ly=y - 56))
    # 記録だけ
    R = (590, 348, 470, 86)
    b.append(card(*R, t["rec"][0], t["rec"][1], dash="6 5", title_size=17, sub_size=13))
    for q, lab in ((Q2, t["shadow_yes"]), (Q3, t["over"])):
        qx = q[0] + q[2] / 2
        b.append(arrow(f"M{qx} {y+h} V{R[1]-4}", label=lab, lx=qx + 10,
                       ly=(y + h + R[1]) / 2 + 5, anchor="start"))
    return svg(W, H, t["title"], "\n".join(b))


# ---------------------------------------------------------------- 構成の図

ARCH = {
    "ja": dict(
        title="会話の記録はPCから出ない。AIに渡すのは質問と、必要な文脈だけ",
        host="あなたのPC — プロセスは1本（run.py）",
        discord=("Discord", "チャンネル・DM"),
        platform=("platforms/discord", "Botの接続\n何体いても1プロセス"),
        core=("core/", "検索・回答生成\n観察ループ・ツールループ"),
        db=("state/archive.db", "SQLite ＋ 全文検索\n全会話がここに残る"),
        dash=("dashboard/", "管理画面（localhost）\n設定・監視・性格"),
        cfg="config.json",
        ai=("Claude Code\nCodex CLI", "あなたが選んだAI"),
        send="質問と、検索で\n引いた文脈だけ"),
    "en": dict(
        title="Your archive stays on your machine — the AI only sees the question and the context it needs",
        host="Your machine — a single process (run.py)",
        discord=("Discord", "channels & DMs"),
        platform=("platforms/discord", "bot connections\nall agents, one process"),
        core=("core/", "search · generation\nobservation & tool loops"),
        db=("state/archive.db", "SQLite + full-text search\nevery message, locally"),
        dash=("dashboard/", "web UI on localhost\nsetup · monitoring · personas"),
        cfg="config.json",
        ai=("Claude Code\nCodex CLI", "the AI you picked"),
        send="question +\ncontext only"),
}


def cylinder(x, y, w, h, title, sub):
    ry = 12
    return "\n".join([
        f'  <path d="M{x} {y+ry} V{y+h-ry} A{w/2} {ry} 0 0 0 {x+w} {y+h-ry} V{y+ry}" '
        f'fill="{CARD}" stroke="{CARD_LINE}" stroke-width="1.5"/>',
        f'  <ellipse cx="{x+w/2}" cy="{y+ry}" rx="{w/2}" ry="{ry}" fill="#1B2542" '
        f'stroke="{CARD_LINE}" stroke-width="1.5"/>',
        text(x + w / 2, y + ry + 38, title, size=18, weight=700, font=MONO),
        text(x + w / 2, y + ry + 64, sub, size=14, color=MUTED),
    ])


def arch(lang):
    t = ARCH[lang]
    W, H = 1280, 560
    b = [eyebrow(40, 58, t["title"])]
    # ホスト（PC）の枠
    hx, hy, hw, hh = 270, 96, 660, 424
    b.append(box(hx, hy, hw, hh, fill="#0E1528", stroke=INDIGO, dash="7 6", rx=22, width=1.6))
    b.append(text(hx + 24, hy + 34, t["host"], size=14, color=INDIGO, weight=700, anchor="start"))
    D = (40, 196, 180, 104)
    PL = (300, 170, 260, 124)
    CO = (630, 158, 270, 148)
    DB = (630, 364, 270, 132)
    DA = (300, 370, 260, 116)
    AI = (1040, 180, 200, 124)
    b.append(card(*D, t["discord"][0], t["discord"][1]))
    b.append(card(*PL, t["platform"][0], t["platform"][1], mono=True, title_size=17))
    b.append(card(*CO, t["core"][0], t["core"][1], mono=True, accent=True, title_size=22, sub_size=15))
    b.append(cylinder(*DB, t["db"][0], t["db"][1]))
    b.append(card(*DA, t["dash"][0], t["dash"][1], mono=True, title_size=17, sub_size=13))
    b.append(card(*AI, t["ai"][0], t["ai"][1], title_size=18))
    cyD, cyP = D[1] + D[3] / 2, PL[1] + PL[3] / 2
    # Discord ⇄ platforms
    b.append(f'  <path d="M{D[0]+D[2]+4} {cyD+2} H{PL[0]-4}" stroke="{CYAN}" stroke-width="2" '
             f'marker-start="url(#arrowAccent)" marker-end="url(#arrowAccent)"/>')
    # platforms ⇄ core
    cyC = CO[1] + 70
    b.append(f'  <path d="M{PL[0]+PL[2]+4} {cyC} H{CO[0]-4}" stroke="{CYAN}" stroke-width="2" '
             f'marker-start="url(#arrowAccent)" marker-end="url(#arrowAccent)"/>')
    # core ⇄ archive
    cx = CO[0] + CO[2] / 2
    b.append(f'  <path d="M{cx} {CO[1]+CO[3]+4} V{DB[1]-4}" stroke="{MUTED}" stroke-width="2" '
             f'marker-start="url(#arrow)" marker-end="url(#arrow)"/>')
    # dashboard → config.json → core
    b.append(arrow(f"M{DA[0]+DA[2]} {DA[1]+40} C{DA[0]+DA[2]+50} {DA[1]+40} "
                   f"{CO[0]-30} {CO[1]+CO[3]-20} {CO[0]-4} {CO[1]+CO[3]-20}", dash="5 5"))
    b.append(text(DA[0] + DA[2] - 10, DA[1] - 12, t["cfg"], size=13, color=MUTED,
                  font=MONO, anchor="end"))
    # core → AI（PCの外へ出るのはここだけ）
    b.append(arrow(f"M{CO[0]+CO[2]} {CO[1]+56} H{AI[0]-4}", accent=True))
    b.append(text((hx + hw + AI[0]) / 2 + 4, CO[1] + 56 + 30, t["send"],
                  size=13, color=CYAN, weight=600))
    return svg(W, H, t["title"], "\n".join(b))


def main():
    for lang in ("ja", "en"):
        for name, fn in (("gate", gate), ("architecture", arch)):
            path = os.path.join(OUT, f"{name}.{lang}.svg")
            with open(path, "w", encoding="utf-8") as f:
                f.write(fn(lang))
            print("wrote", os.path.relpath(path))


if __name__ == "__main__":
    main()
