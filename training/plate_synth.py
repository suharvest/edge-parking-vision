#!/usr/bin/env python3
"""合成车牌生成器（NRD-3 §7「合成车牌生成器」，§8 P3）。

本期只做中国大陆制式（§0.1）：蓝牌 440×140（比例）、新能源绿牌 480×140。
德国牌模板与 Alte DIN 1451 字体本期不做；字母数字暂用 Noto Sans CJK SC
绘制（与真实 GA 36 字形不同，靠真实数据弥补，§7 同款备注）。

- 模板用 PIL 程序绘制，不使用任何第三方车牌图片或模板文件。
- 号码随机：按 §6.4 正则生成，省份均匀分布（弥补 CCPD 以皖为主的偏差）。
- 退化：透视（±15°）、模糊、噪声、亮度/对比度、夜间补光过曝、JPEG。
  贴背景只用纯色噪声背景，不用 COCO 图片（避免再分发问题）。
- 只在训练环境运行（spark）；PIL 不进 ``core_parking`` 与运行镜像。

用法::

    python training/plate_synth.py --region cn --n 100 --out /tmp/s
    python training/plate_synth.py --region cn --n 20 --out /tmp/s \
        --mode whole --seed 42        # 检测用整图（噪声背景）

字体：``training/fonts/NotoSansCJKsc-Bold.otf``（下载见 training/fonts/README.md，
SIL OFL-1.1）。文件名包含归一化号码：``00001_京A12345.jpg``。
"""
from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core-py"))
from core_parking.plate.charset import CN_LETTERS, CN_PROVINCES, DIGITS  # noqa: E402

BLUE = (11, 61, 145)          # #0B3D91（§7）
GREEN_TOP = (232, 244, 220)   # 渐变绿底，上浅下深
GREEN_BOTTOM = (146, 194, 128)
SS = 2                        # 超采样倍数（抗锯齿）
FONT_FILE = "NotoSansCJKsc-Bold.otf"
LD = CN_LETTERS + DIGITS


def random_plate(rng: random.Random) -> tuple[str, str]:
    """按 §6.4 正则随机生成；省份均匀分布。返回 (plate, plate_type)。"""
    ptype = rng.choice(("cn_blue", "cn_green"))
    n = 5 if ptype == "cn_blue" else 6
    plate = rng.choice(CN_PROVINCES) + rng.choice(CN_LETTERS) + "".join(rng.choice(LD) for _ in range(n))
    return plate, ptype


def _fit_font(font_path: str, text: str, target_w: int, start: int) -> ImageFont.FreeTypeFont:
    size = start
    while size > 8:
        f = ImageFont.truetype(font_path, size)
        if f.getbbox(text)[2] - f.getbbox(text)[0] <= target_w:
            return f
        size -= 2
    return ImageFont.truetype(font_path, 8)


def draw_plate(plate: str, ptype: str, font_path: str) -> Image.Image:
    """绘制车牌底板与字符（2× 超采样后缩小）。"""
    w_mm, h_mm = (440, 140) if ptype == "cn_blue" else (480, 140)
    w, h = w_mm * SS, h_mm * SS
    img = Image.new("RGB", (w, h))
    if ptype == "cn_blue":
        img.paste(BLUE, (0, 0, w, h))
    else:  # 渐变绿底
        for y in range(h):
            t = y / (h - 1)
            img.paste(tuple(round(a + (b - a) * t) for a, b in zip(GREEN_TOP, GREEN_BOTTOM)), (0, y, w, y + 1))
    draw = ImageDraw.Draw(img)
    ink = (255, 255, 255) if ptype == "cn_blue" else (0, 0, 0)
    if ptype == "cn_blue":  # 白边框
        m = 6 * SS
        draw.rectangle([0, 0, w - 1, h - 1], outline=ink, width=3 * SS)
    # 省份 + 第二字符之间留间隔号位（不绘制点，识别不含 ·）
    body = plate[1] + "  " + plate[2:]
    size = round(h * 0.62)
    f_prov = _fit_font(font_path, plate[0], round(w * 0.16), size)
    f_body = _fit_font(font_path, body, round(w * 0.74), size)
    for text, font, box in (
        (plate[0], f_prov, (round(w * 0.05), round(h * 0.16), round(w * 0.22), round(h * 0.88))),
        (body, f_body, (round(w * 0.22), round(h * 0.16), round(w * 0.97), round(h * 0.88))),
    ):
        draw.text(box[:2], text, font=font, fill=ink, anchor="lt")
    return img.resize((w_mm, h_mm), Image.LANCZOS)


def _perspective_coeffs(src: list[tuple[float, float]], dst: list[tuple[float, float]]) -> list[float]:
    """解 8×8 线性方程求 src→dst 单应性系数（纯 Python 高斯消元）。"""
    a = [[1.0] * 8 for _ in range(8)]
    b = [0.0] * 8
    for i, ((sx, sy), (dx, dy)) in enumerate(zip(src, dst)):
        a[2 * i][2] = sx
        a[2 * i][3] = sy
        a[2 * i][4] = 1.0
        b[2 * i] = dx
        a[2 * i + 1][0] = sx
        a[2 * i + 1][1] = sy
        a[2 * i + 1][5] = 1.0
        b[2 * i + 1] = dy
    for col in range(8):
        piv = next(r for r in range(col, 8) if abs(a[r][col]) > 1e-9)
        a[col], a[piv] = a[piv], a[col]
        b[col], b[piv] = b[piv], b[col]
        for r in range(8):
            if r != col and a[r][col]:
                f = a[r][col] / a[col][col]
                for c in range(col, 8):
                    a[r][c] -= f * a[col][c]
                b[r] -= f * b[col]
    return [b[i] / a[i][i] for i in range(8)]


def _quad_jitter(rng: random.Random, w: int, h: int, canvas_w: int, canvas_h: int,
                 max_deg: float = 15.0) -> list[tuple[float, float]]:
    """在画布内取目标四角：±15° 透视等价的随机四边形。"""
    scale = rng.uniform(0.55, 0.85)
    cx, cy = canvas_w / 2, canvas_h / 2
    hw, hh = w * scale / 2, h * scale / 2
    lim = max_deg / 90.0
    pts = []
    for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
        dx = hw * (1 + rng.uniform(-lim, lim))
        dy = hh * (1 + rng.uniform(-lim, lim))
        pts.append((cx + sx * dx, cy + sy * dy))
    return pts


def degrade(img: Image.Image, rng: random.Random) -> Image.Image:
    """模糊、噪声、亮度/对比度、夜间补光过曝（§7 退化清单；雨雾由模糊近似）。"""
    if rng.random() < 0.6:
        img = img.filter(ImageFilter.GaussianBlur(rng.uniform(0.3, 2.2)))
    if rng.random() < 0.5:  # 噪声
        noise = Image.effect_noise(img.size, rng.uniform(8, 40)).convert("RGB")
        img = Image.blend(img, noise, rng.uniform(0.05, 0.25))
    img = ImageEnhance.Brightness(img).enhance(rng.uniform(0.55, 1.0))
    img = ImageEnhance.Contrast(img).enhance(rng.uniform(0.7, 1.3))
    if rng.random() < 0.2:  # 夜间补光过曝
        img = ImageEnhance.Brightness(img).enhance(rng.uniform(1.6, 2.4))
        img = img.filter(ImageFilter.GaussianBlur(1.5))
    return img


def synth_one(plate: str, ptype: str, font_path: str, rng: random.Random,
              mode: str = "crop") -> Image.Image:
    base = draw_plate(plate, ptype, font_path)
    if mode == "whole":
        cw, ch = 640, 480
        canvas = Image.effect_noise((cw, ch), 32).convert("RGB")
        src = [(0.0, 0.0), (float(base.width), 0.0), (float(base.width), float(base.height)), (0.0, float(base.height))]
        dst = _quad_jitter(rng, base.width, base.height, cw, ch)
        coeffs = _perspective_coeffs(src, dst)
        warped = base.transform((cw, ch), Image.PERSPECTIVE, coeffs, Image.BICUBIC)
        mask = Image.new("L", (base.width, base.height), 255).transform((cw, ch), Image.PERSPECTIVE, coeffs)
        canvas.paste(warped, (0, 0), mask)
        img = canvas
    else:
        img = base
    return degrade(img, rng)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--region", default="cn", choices=["cn"], help="本期只做 cn（§0.1）")
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--out", required=True)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--mode", default="crop", choices=["crop", "whole"],
                    help="crop=识别样本，whole=检测用整图（噪声背景）")
    ap.add_argument("--font", default=None, help="覆盖字体路径")
    args = ap.parse_args()

    font_path = args.font or str(Path(__file__).parent / "fonts" / FONT_FILE)
    if not Path(font_path).exists():
        print(f"字体缺失: {font_path}\n下载方法见 training/fonts/README.md", file=sys.stderr)
        return 2

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)
    for i in range(args.n):
        plate, ptype = random_plate(rng)
        img = synth_one(plate, ptype, font_path, rng, args.mode)
        img.save(out / f"{i:05d}_{plate}_{ptype}.jpg", quality=rng.randint(60, 95))
    print(f"{args.n} 张 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
