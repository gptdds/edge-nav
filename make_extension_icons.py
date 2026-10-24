# -*- coding: utf-8 -*-
"""生成扩展图标（16/48/128），纯标准库，不依赖 Pillow。

图案：深色圆角方块 + 三条横线（第一条实心，其余半透明），像一组书签条目。
和页面同色系（#1c1c1a / #e8e7e3），放一起不违和。
"""
import os
import struct
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "extension")

BG = (28, 28, 26)
FG = (232, 231, 227)


def in_rounded_rect(x, y, w, h, r, px, py):
    """像素 (px,py) 是否落在圆角矩形内。"""
    if not (x <= px < x + w and y <= py < y + h):
        return False
    dx = 0
    if px < x + r:
        dx = x + r - px
    elif px > x + w - r - 1:
        dx = px - (x + w - r - 1)
    dy = 0
    if py < y + r:
        dy = y + r - py
    elif py > y + h - r - 1:
        dy = py - (y + h - r - 1)
    return dx * dx + dy * dy <= r * r


def encode_png(w, h, rgba):
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    stride = w * 4
    raw = b"".join(b"\x00" + rgba[y * stride:(y + 1) * stride] for y in range(h))
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)   # 8bit RGBA
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def make_icon(size: int) -> bytes:
    buf = bytearray(size * size * 4)          # 全透明起手
    r = max(2, round(size * 0.22))

    # 圆角底
    for py in range(size):
        for px in range(size):
            if in_rounded_rect(0, 0, size, size, r, px, py):
                o = (py * size + px) * 4
                buf[o:o + 4] = bytes(BG + (255,))

    # 三条横条：第一条实心，后面两条渐淡
    bar_h = max(1, round(size * 0.08))
    bar_r = bar_h / 2.0
    bx = round(size * 0.27)
    bw = round(size * 0.46)
    for i, fy in enumerate((0.335, 0.465, 0.595)):
        by = round(size * fy)
        alpha = 1.0 if i == 0 else (0.62 if i == 1 else 0.36)
        for py in range(by, by + bar_h):
            for px in range(bx, bx + bw):
                if in_rounded_rect(bx, by, bw, bar_h, bar_r, px, py):
                    o = (py * size + px) * 4
                    for k in range(3):
                        buf[o + k] = round(buf[o + k] * (1 - alpha) + FG[k] * alpha)
                    buf[o + 3] = 255

    return encode_png(size, size, bytes(buf))


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    for s in (16, 48, 128):
        path = os.path.join(OUT_DIR, "icon%d.png" % s)
        data = make_icon(s)
        with open(path, "wb") as fh:
            fh.write(data)
        print("  icon%-4d %4d 字节  %s" % (s, len(data), path))


if __name__ == "__main__":
    main()
