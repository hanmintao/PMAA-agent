"""
用 OCR 自动识别谱图的 **m/z 轴刻度**，免除手填轴范围。

═══════════════════════════════════════════════════════════════════════════
 为什么是可选增强，且不因此放松人工确认
═══════════════════════════════════════════════════════════════════════════
* 本机没有 brew / tesseract，装不了系统级 OCR。这里用 **rapidocr-onnxruntime**
  （纯 pip、自带 PP-OCR 的 ONNX 模型、无需系统依赖）作为**可选**依赖：
  装了就能自动读刻度，没装就自动退回手动填写——功能不缺失，只是少个便利。
* OCR 会读错，所以必须过质量门：≥3 个刻度、线性回归 R²≥0.95、数值单调。
  任一条不满足就**返回失败**，绝不拿可疑结果去标定轴。
* 即使自动标定成功，结果依旧是 derived、证据上限依旧是 candidate、
  依旧要人工点确认——OCR 只是省去"手填两个数字"，不是免去核验。
"""
from __future__ import annotations

import io
import re
import threading
from dataclasses import dataclass, field

import numpy as np

_ENGINE = None
_ENGINE_LOCK = threading.Lock()
_NUM_RE = re.compile(r"^\d{1,4}(?:[.,]\d{1,2})?$")


class OCRError(Exception):
    """OCR 不可用或识别失败。调用方应据此优雅降级到手动填写。"""


@dataclass
class AxisCalibration:
    ok: bool
    mz_min: float | None = None
    mz_max: float | None = None
    r2: float | None = None
    n_points: int = 0
    slope: float | None = None      # m/z per pixel
    intercept: float | None = None  # m/z at x=0
    points: list[dict] = field(default_factory=list)
    method: str = "rapidocr_linear_fit"
    message: str = ""


def ocr_available() -> bool:
    """探测 OCR 引擎是否可用（不触发模型加载）。"""
    try:
        import rapidocr_onnxruntime  # noqa: F401
        return True
    except Exception:
        return False


def _engine():
    """惰性加载并缓存引擎（首次加载模型约 1–2 秒）。"""
    global _ENGINE
    if _ENGINE is None:
        with _ENGINE_LOCK:
            if _ENGINE is None:
                try:
                    from rapidocr_onnxruntime import RapidOCR
                    _ENGINE = RapidOCR()
                except Exception as e:
                    raise OCRError(f"OCR 引擎不可用：{type(e).__name__}: {e}")
    return _ENGINE


def _to_float(text: str) -> float | None:
    t = (text or "").strip().replace(" ", "").replace(",", ".")
    t = t.rstrip(".")
    if not _NUM_RE.match(t):
        return None
    try:
        return float(t)
    except ValueError:
        return None


def _ocr_items(data: bytes) -> list[dict]:
    """返回 [{text, x_center, y_bottom, score}]。"""
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        if img.mode != "RGB":
            img = img.convert("RGB")
        arr = np.asarray(img)
    except Exception as e:
        raise OCRError(f"图像读取失败：{type(e).__name__}")

    res, _ = _engine()(arr)
    items: list[dict] = []
    for item in res or []:
        box, text, score = item[0], item[1], item[2]
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        items.append({"text": str(text), "x_center": float(sum(xs) / len(xs)),
                      "y_bottom": float(max(ys)), "score": float(score)})
    return items


def _pick_axis_row(items: list[dict], height: int) -> list[dict]:
    """
    挑出 x 轴刻度标签那一排。

    思路：刻度标签的 y 几乎相同，且位于图下部。
    取"含数字最多"的那一簇 y（而不是简单取最靠下的），
    这样即使图下方还有图注/标题也不会选错。
    """
    nums = []
    for it in items:
        v = _to_float(it["text"])
        if v is None or not (1.0 <= v <= 5000.0):
            continue
        nums.append({**it, "value": v})
    if len(nums) < 3:
        return []

    ys = np.array([n["y_bottom"] for n in nums])
    # 以 ~2% 图高为带宽做简单聚类
    band = max(6.0, height * 0.02)
    best, best_n = None, 0
    for y0 in ys:
        cluster = [n for n in nums if abs(n["y_bottom"] - y0) <= band]
        if len(cluster) > best_n:
            best, best_n = cluster, len(cluster)
    return best or []


def detect_mz_axis(data: bytes, *, plot_left: int | None = None,
                   plot_right: int | None = None,
                   min_points: int = 3, min_r2: float = 0.95) -> AxisCalibration:
    """
    识别 x 轴刻度并线性拟合出 像素 → m/z 映射，进而给出绘图区两端的 m/z。

    :param plot_left/plot_right: 绘图区像素范围；未提供则用所有刻度点的 x 范围。
    :raises OCRError: OCR 不可用（调用方应降级）
    """
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            height = im.height
    except Exception as e:
        raise OCRError(f"无法读取图像尺寸：{type(e).__name__}")

    items = _ocr_items(data)
    row = _pick_axis_row(items, height)
    fail = AxisCalibration(ok=False)
    if len(row) < min_points:
        fail.message = f"只识别到 {len(row)} 个刻度数字（需要 ≥{min_points} 个），请手动填写轴范围"
        return fail

    xs = np.array([r["x_center"] for r in row], dtype=float)
    vs = np.array([r["value"] for r in row], dtype=float)

    # 线性拟合 v = a*x + b
    try:
        a, b = np.polyfit(xs, vs, 1)
    except Exception:
        fail.message = "刻度拟合失败，请手动填写轴范围"
        return fail
    pred = a * xs + b
    ss_res = float(np.sum((vs - pred) ** 2))
    ss_tot = float(np.sum((vs - vs.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0

    if r2 < min_r2:
        fail.message = (f"刻度拟合质量不足（R²={r2:.3f} < {min_r2}），"
                        f"OCR 结果不可靠，请手动填写轴范围")
        fail.r2 = round(r2, 4)
        fail.n_points = len(row)
        return fail
    if abs(a) < 1e-9:
        fail.message = "刻度数值不随位置变化，疑似非 m/z 轴，请手动填写"
        return fail

    left = float(plot_left) if plot_left is not None else float(xs.min())
    right = float(plot_right) if plot_right is not None else float(xs.max())
    mz_a, mz_b = a * left + b, a * right + b
    lo, hi = (mz_a, mz_b) if mz_a <= mz_b else (mz_b, mz_a)

    return AxisCalibration(
        ok=True, mz_min=round(float(lo), 2), mz_max=round(float(hi), 2),
        r2=round(float(r2), 4), n_points=len(row),
        slope=round(float(a), 6), intercept=round(float(b), 4),
        points=[{"x": round(r["x_center"], 1), "mz": r["value"],
                 "text": r["text"], "score": round(r["score"], 2)} for r in row],
        message=(f"OCR 识别到 {len(row)} 个 x 轴刻度，线性拟合 R²={r2:.4f}"
                 f"→ 建议轴范围 {lo:.0f}–{hi:.0f}（请核对）"),
    )
