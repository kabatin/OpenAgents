#!/usr/bin/env python3
"""stdout / stderr の各行に時刻を付ける（v4 Phase 0 計測）。

BOTのログは常駐化の仕組み（launchd 等）が stdout をそのまま落としているだけで、
行に時刻が無く「いつ何が起きたか」を事後に追えなかった。print を全部書き換える
代わりにストリームを1箇所でラップする。stderr も包むので Traceback にも時刻が付く。"""

import sys
import time

_FMT = "%m-%d %H:%M:%S"


def stamp_lines(text, at_line_start, now_str):
    """text の各行頭に now_str を付ける（純粋関数・テスト対象）。

    at_line_start: 直前の出力が改行で終わっていたか（途中行に印を挟まない）。
    Returns: (出力文字列, 次回の at_line_start)
    """
    if not text:
        return "", at_line_start
    out = []
    for piece in text.splitlines(keepends=True):
        if at_line_start and piece.strip():
            out.append(f"{now_str} {piece}")
        else:
            out.append(piece)
        at_line_start = piece.endswith("\n")
    return "".join(out), at_line_start


class StampedStream:
    """書き込みごとに行頭へ時刻を付ける薄いラッパ。他属性は元ストリームへ委譲。"""

    def __init__(self, stream):
        self._stream = stream
        self._at_line_start = True

    def write(self, text):
        out, self._at_line_start = stamp_lines(
            text, self._at_line_start, time.strftime(_FMT))
        return self._stream.write(out)

    def flush(self):
        self._stream.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


def install():
    """sys.stdout / sys.stderr を包む（冪等）。"""
    if not isinstance(sys.stdout, StampedStream):
        sys.stdout = StampedStream(sys.stdout)
    if not isinstance(sys.stderr, StampedStream):
        sys.stderr = StampedStream(sys.stderr)
