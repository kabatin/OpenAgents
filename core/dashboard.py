#!/usr/bin/env python3
"""週次ダッシュボードの画像化（RM#76）。

週次レポートの数字を横棒グラフPNGにして添付する（Discordはインライン表示する）。
日本語ラベルを描くため Pillow ＋ macOS標準フォントを使う。Pillowが無い環境では
zlib/struct だけの簡易PNG（数値と色帯のみ）へ自動フォールバックし、
グラフが出せないだけで週次レポート自体は必ず投稿される。

数字の正確さが要るのでLLM画像生成は使わない（棒の長さ＝実データ）。

単体テスト: ./venv/bin/python -m unittest test_batch_pack2 -v
"""

import os
import struct
import zlib

# 日本語が描けるフォント候補（先に見つかったものを使う）
FONT_CANDIDATES = [
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",
]

WIDTH = 700
BAR_H = 26
GAP = 12
PAD = 16
LABEL_W = 260
TITLE_H = 34
BG = (255, 255, 255)
FG = (34, 34, 34)
SUB = (120, 120, 120)
GRID = (232, 232, 232)
COLORS = {"spoke": (14, 122, 104), "silent": (154, 165, 161),
          "nudge": (201, 138, 43), "decisions": (58, 110, 165),
          "golden": (122, 94, 165)}
MARKS = {"spoke": "🟩", "silent": "⬜", "nudge": "🟧",
         "decisions": "🟦", "golden": "🟪"}


def find_font():
    """日本語フォントのパス（無ければ None）。"""
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            return path
    return None


def build_rows(stats, roster):
    """グラフ化する行 [(ラベル, 値, 色キー)] を作る（純粋関数）。"""
    rows = []
    agents = stats.get("agents") or {}
    for a in roster:
        s = agents.get(a["id"]) or {}
        spoke = sum((s.get("spoke_by_kind") or {}).values())
        rows.append((f"{a['name']} 自発発言", spoke, "spoke"))
        rows.append((f"{a['name']} 沈黙判定", s.get("silent", 0), "silent"))
    nudge = sum((agents.get(a["id"]) or {}).get("nudge", 0) for a in roster)
    rows.append(("納期の声かけ", nudge, "nudge"))
    rows.append(("決定事項（累計）", stats.get("decisions_total", 0),
                 "decisions"))
    rows.append(("ゴールデン（累計）", stats.get("golden_total", 0), "golden"))
    return rows


def render_png(rows, subtitle="", title="週次レポート"):
    """横棒グラフPNGのバイト列。Pillowがあれば日本語ラベル入り。"""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return _render_png_fallback(rows, subtitle)
    font_path = find_font()
    if font_path is None:
        return _render_png_fallback(rows, subtitle)

    height = PAD * 2 + TITLE_H + len(rows) * (BAR_H + GAP)
    img = Image.new("RGB", (WIDTH, height), BG)
    d = ImageDraw.Draw(img)
    f_title = ImageFont.truetype(font_path, 17)
    f_label = ImageFont.truetype(font_path, 13)
    f_value = ImageFont.truetype(font_path, 13)

    d.text((PAD, PAD), title, font=f_title, fill=FG)
    if subtitle:
        d.text((PAD + d.textlength(title, font=f_title) + 10, PAD + 3),
               subtitle, font=f_label, fill=SUB)

    max_v = max([v for _, v, _ in rows] + [1])
    bar_area = WIDTH - LABEL_W - PAD - 70
    y = PAD + TITLE_H
    for label, value, ckey in rows:
        color = COLORS.get(ckey, (100, 100, 100))
        d.text((PAD, y + 6), label, font=f_label, fill=FG)
        d.rectangle([LABEL_W, y, LABEL_W + bar_area, y + BAR_H], fill=GRID)
        w = int(bar_area * value / max_v) if max_v else 0
        d.rectangle([LABEL_W, y, LABEL_W + max(w, 3), y + BAR_H], fill=color)
        d.text((LABEL_W + max(w, 3) + 8, y + 6), str(value), font=f_value,
               fill=FG)
        y += BAR_H + GAP
    from io import BytesIO
    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ------------------------------------------------ フォールバック（依存ゼロ）

def _png_bytes(width, height, px):
    """RGBバッファ→PNG（zlib+structのみ）。"""
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        off = y * width * 3
        raw += px[off:off + width * 3]

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


def _render_png_fallback(rows, subtitle=""):
    """Pillow/フォントが無い環境用: 色帯と棒だけの簡易グラフ（文字なし）。
    ラベルは本文の凡例（legend_lines）が担う。"""
    height = PAD * 2 + len(rows) * (BAR_H + GAP)
    px = bytearray(bytes(BG) * WIDTH * height)

    def rect(x, y, w, h, color):
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(WIDTH, x + w), min(height, y + h)
        row = bytes(color) * max(0, x1 - x0)
        for yy in range(y0, y1):
            off = (yy * WIDTH + x0) * 3
            px[off:off + len(row)] = row

    max_v = max([v for _, v, _ in rows] + [1])
    bar_area = WIDTH - PAD * 2 - 40
    y = PAD
    for _label, value, ckey in rows:
        color = COLORS.get(ckey, (100, 100, 100))
        w = int(bar_area * value / max_v) if max_v else 0
        rect(PAD, y, max(w, 3), BAR_H, color)
        y += BAR_H + GAP
    return _png_bytes(WIDTH, height, px)


def build_chart_file(stats, roster, subtitle, out_dir):
    """PNGを書き出してパスを返す（中身が無ければ None）。"""
    rows = build_rows(stats, roster)
    if not any(v for _, v, _ in rows):
        return None
    path = os.path.join(out_dir, "weekly_report.png")
    with open(path, "wb") as f:
        f.write(render_png(rows, subtitle))
    return path


def has_labels():
    """画像内に日本語ラベルを描けるか（本文の凡例を出すか判断する）。"""
    try:
        import PIL
    except ImportError:
        return False
    return bool(PIL) and find_font() is not None


def legend_lines(rows):
    """フォールバック時に本文へ添える凡例（全行を上から順に列挙）。"""
    return [f"{MARKS.get(ckey, '▪️')}{label}" for label, _v, ckey in rows]
