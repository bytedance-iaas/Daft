"""生成质检台图标:Arco 蓝圆角方块 + 白色漩涡(照 Daft 那枚的手感重画)。

产出 curation/ui/assets/favicon.png(仓库里已有一份成品,改参数才需要重跑):
    python3 scripts/make_favicon.py curation/ui/assets/favicon.png 64 1.75 3.6 8.6 25.0

用法:python3 make_favicon.py <输出路径> <边长px> [turns] [_] [w_max] [r1]
纯 stdlib(zlib+struct 手写 PNG),不引任何图形库。
"""
import math
import struct
import sys
import zlib

out = sys.argv[1]
N = int(sys.argv[2])
TURNS = float(sys.argv[3]) if len(sys.argv) > 3 else 1.9
float(sys.argv[4]) if len(sys.argv) > 4 else 0            # (占位,旧参数)
W1F = float(sys.argv[5]) if len(sys.argv) > 5 else 8.6     # 最粗处笔宽(/64 基准)
R1F = float(sys.argv[6]) if len(sys.argv) > 6 else 24.0     # 外缘半径

SS = 4
W = N * SS
S = N / 64.0                      # 一切尺寸按 64px 基准等比放大
R = 12 * S * SS
BLUE = (0x16, 0x5D, 0xFF)
C = W / 2

TH_MAX = TURNS * 2 * math.pi
R0, R1 = 3.0 * S * SS, R1F * S * SS
WMAX = W1F * S * SS       # 峰值笔宽(两端收成尖,见 width_at)



def width_at(f):
    """笔宽随进度变化。**两端都收到 0**(Daft 那枚漩涡的头尾都是尖的,
    不是平截口/圆头 —— 2026-08-13 用户点名看仔细点)。
    形状取 f^a·(1-f)^b 并归一到峰值 = WMAX:a<b ⇒ 峰值偏外侧,
    尾巴(中心那头)细而长,与原图的手感一致。"""
    a, b = 0.55, 0.22
    peak = (a / (a + b)) ** a * (b / (a + b)) ** b
    return WMAX * (f ** a) * ((1 - f) ** b) / peak


bg = [[0] * N for _ in range(N)]
fg = [[0] * N for _ in range(N)]


def in_round_rect(x, y):
    if R <= x <= W - R or R <= y <= W - R:
        return 0 <= x <= W and 0 <= y <= W
    cx = min(max(x, R), W - R)
    cy = min(max(y, R), W - R)
    return math.hypot(x - cx, y - cy) <= R


for yy in range(W):
    for xx in range(W):
        if in_round_rect(xx + .5, yy + .5):
            bg[yy // SS][xx // SS] += 1

steps = int(2600 * S)
for i in range(steps + 1):
    th = TH_MAX * i / steps
    f = th / TH_MAX
    r = R0 + (R1 - R0) * f
    rad = width_at(f) / 2
    px = C + r * math.cos(th - math.pi / 2)
    py = C + r * math.sin(th - math.pi / 2)
    for yy in range(max(0, int(py - rad)), min(W, int(py + rad) + 2)):
        for xx in range(max(0, int(px - rad)), min(W, int(px + rad) + 2)):
            if (xx + .5 - px) ** 2 + (yy + .5 - py) ** 2 <= rad * rad:
                fg[yy // SS][xx // SS] += 1

n2 = SS * SS
raw = bytearray()
for r_ in range(N):
    raw.append(0)
    for c in range(N):
        a = min(1.0, bg[r_][c] / n2)
        t = min(1.0, fg[r_][c] / n2)
        col = [round(ch + (255 - ch) * t) for ch in BLUE]
        raw += bytes(col) + bytes([round(255 * a)])


def chunk(tag, data):
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))


png = (b"\x89PNG\r\n\x1a\n"
       + chunk(b"IHDR", struct.pack(">IIBBBBB", N, N, 8, 6, 0, 0, 0))
       + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
       + chunk(b"IEND", b""))
open(out, "wb").write(png)
print(out, N, len(png), "bytes")
