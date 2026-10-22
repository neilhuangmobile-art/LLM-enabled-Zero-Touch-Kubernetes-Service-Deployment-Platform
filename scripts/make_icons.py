"""產生 PWA 圖示與 favicon（2026-10-22）。改設計後重跑：python scripts/make_icons.py

圖案：深海藍底、訊號藍的「0」字環，右上一個綠點（零接觸 + 有人把關），跟網站的主控台配色一致。
maskable 版本的圖案縮在中間 60% 安全區，讓 Android 裁成圓形或圓角方形時不會被切掉。
"""
import os

from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "static", "icons")
VOID, PANEL, SIGNAL, PASS = (11, 22, 34), (20, 42, 60), (92, 200, 255), (61, 214, 140)


def draw(size: int, scale: float, rounded: bool) -> Image.Image:
    s = size * 4  # 先畫大張再縮小，邊緣才平滑
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if rounded:
        d.rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * 0.22), fill=VOID)
    else:
        d.rectangle([0, 0, s, s], fill=VOID)
    c, r = s / 2, s * 0.30 * scale
    w = max(4, int(s * 0.085 * scale))
    d.ellipse([c - r - w, c - r * 1.18 - w, c + r + w, c + r * 1.18 + w], fill=PANEL)
    d.ellipse([c - r, c - r * 1.18, c + r, c + r * 1.18], outline=SIGNAL, width=w)
    # 斜線：數字 0 的辨識記號
    d.line([c + r * 0.42, c - r * 0.62, c - r * 0.42, c + r * 0.62], fill=SIGNAL, width=int(w * 0.7))
    dot = s * 0.085 * scale
    dx, dy = c + r * 0.98, c - r * 1.05
    d.ellipse([dx - dot - w * 0.5, dy - dot - w * 0.5, dx + dot + w * 0.5, dy + dot + w * 0.5], fill=VOID)
    d.ellipse([dx - dot, dy - dot, dx + dot, dy + dot], fill=PASS)
    return img.resize((size, size), Image.LANCZOS)


def main():
    os.makedirs(OUT, exist_ok=True)
    draw(192, 1.0, True).save(os.path.join(OUT, "icon-192.png"))
    draw(512, 1.0, True).save(os.path.join(OUT, "icon-512.png"))
    draw(512, 0.72, False).save(os.path.join(OUT, "maskable-512.png"))
    draw(180, 0.9, False).convert("RGB").save(os.path.join(OUT, "apple-touch-180.png"))
    draw(96, 1.0, True).save(os.path.join(OUT, "badge-96.png"))
    draw(64, 1.1, True).save(os.path.join(ROOT, "static", "favicon.ico"), sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])
    print("已產生：", sorted(os.listdir(OUT)))


if __name__ == "__main__":
    main()
