"""
从质谱图**图片**中反解峰（棒图 / 实心填充图）。

═══════════════════════════════════════════════════════════════════════════
 科学边界（务必遵守，整个平台的立身之本）
═══════════════════════════════════════════════════════════════════════════
从图片反解出来的峰是 **derived（推导）** 数据，不是 observed（实测）数据：

  * 相对强度只是图像像素估计——压缩、文字重叠、基线和裁切都会造成偏差；
  * m/z **必须**由用户校准（给出轴的 m/z 范围），否则无法从像素映射；
  * 本机未安装 OCR，轴刻度数字无法自动识别，所以不做"猜刻度"这种事；
  * 结果一律标记 basis="derived_from_image"，入库前必须人工确认；
  * 证据上限被压到 candidate，且额外标注 IMAGE_DERIVED 缺失项。

换句话说：本模块负责"把图读成候选峰"，**不负责**"把候选峰说成实测值"。
"""
from __future__ import annotations

import io
import math
from dataclasses import dataclass, field

import numpy as np
from PIL import Image


@dataclass
class ExtractedPeak:
    mz: float | None            # 校准前为 None
    x_pixel: int
    relative_intensity: float   # 相对基峰 = 100
    raw_height: float


@dataclass
class ImageExtractionResult:
    peaks: list[ExtractedPeak]
    image_width: int
    image_height: int
    plot_left: int
    plot_right: int
    baseline_y: int
    params: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    confidence: str = "low"     # low / medium；永远不可能是 high


def _load_gray(data: bytes) -> np.ndarray:
    """转灰度并返回 float 数组（0=黑, 255=白）。"""
    img = Image.open(io.BytesIO(data))
    if img.mode not in ("L", "RGB", "RGBA", "P", "CMYK", "LA"):
        img = img.convert("RGB")
    if img.mode != "L":
        img = img.convert("L")
    return np.asarray(img, dtype=np.float32)


def _foreground_mask(gray: np.ndarray) -> np.ndarray:
    """
    前景 = 比背景暗的像素。背景用图像四角的亮度中位数稳健估计，
    避免彩色谱图转灰度后基线漂移。
    """
    h, w = gray.shape
    edge = np.concatenate([
        gray[0, :].ravel(), gray[-1, :].ravel(),
        gray[:, 0].ravel(), gray[:, -1].ravel(),
    ])
    bg = float(np.median(edge))
    # 前景阈值：比背景暗至少 25%（对淡色扫描件也够用，同时不吃掉抗锯齿噪点）
    thr = bg * 0.75 if bg > 40 else bg - 12
    return gray < max(thr, 5.0)


def _column_profile(mask: np.ndarray) -> tuple[np.ndarray, int]:
    """
    对每一列求"最顶部前景像素" → 高度剖面，基线由**底部像素的众数**确定。

    为什么用底部众数而不是图片底边：
      真图的绘图区四周常有坐标轴、刻度、留白，图片底边 ≠ 谱图基线。
      直接拿 h-1 当基线会给每个峰都加上一个固定偏移，
      表现为"矮峰的相对强度被系统性抬高"（实测可达 +12 个百分点）。
    而**底部像素的众数**对三种画法都成立：
      ① 画了基线横线   —— 绝大多数列的底部都落在基线上；
      ② 没画横线       —— 所有峰的底部本身就是基线；
      ③ 轮廓/填充峰    —— 底边同样是基线。
    """
    h, w = mask.shape
    prof = np.zeros(w, dtype=np.float32)
    has = np.any(mask, axis=0)
    if not has.any():
        return prof, h - 1

    cols = np.nonzero(has)[0]
    # 每列第一个 / 最后一个前景像素的行号
    first_row = np.argmax(mask, axis=0)
    last_row = h - 1 - np.argmax(mask[::-1, :], axis=0)

    bottoms = last_row[cols].astype(np.int64)
    counts = np.bincount(bottoms, minlength=h)
    baseline = int(np.argmax(counts))
    # 兜底：若众数只占极小比例（说明底边杂乱），退回用最低前景像素
    if counts[baseline] < max(3, 0.2 * cols.size):
        baseline = int(bottoms.max())

    tops = first_row[cols].astype(np.float32)
    heights = baseline - tops
    heights = np.clip(heights, 0.0, None)
    prof[cols] = heights
    return prof, baseline


def _true_runs(row: np.ndarray) -> list[tuple[int, int]]:
    """返回布尔行中连续 True 区间（含端点）。"""
    changes = np.diff(np.r_[False, row, False].astype(np.int8))
    starts = np.where(changes == 1)[0]
    ends = np.where(changes == -1)[0]
    return [(int(a), int(b - 1)) for a, b in zip(starts, ends)]


def _heights_attached_to_axis(mask: np.ndarray, baseline: int,
                              left: int, right: int) -> np.ndarray:
    """
    只量取与横轴相连的竖线高度。

    真实数据库截图常同时含标题、结构式、按钮、数字标签和边框。若直接取每列最顶部黑点，
    上述文字会被当成峰；从横轴向上追踪则会在文字与峰线之间的空白处停止。
    """
    h, w = mask.shape
    heights = np.zeros(right - left + 1, dtype=np.float32)
    for offset, x in enumerate(range(left, right + 1)):
        y = baseline - 1
        top = baseline
        gap = 0
        while y >= 0:
            # 允许 1 像素抗锯齿断点，并查看左右相邻列以兼容细绿线/斜边。
            hit = bool(mask[y, max(0, x - 1):min(w, x + 2)].any())
            if hit:
                top = y
                gap = 0
            else:
                gap += 1
                if gap > 1:
                    break
            y -= 1
        heights[offset] = max(0, baseline - top)
    return heights


def _axis_attached_profile(mask: np.ndarray) -> tuple[np.ndarray, int, int, int] | None:
    """在含页面文字/边框的截图中寻找最像质谱横轴的长线并构建连接高度剖面。"""
    h, w = mask.shape
    min_run = max(40, int(w * 0.35))
    best: tuple[float, int, int, int, np.ndarray] | None = None

    # 忽略最上方标题区和最底部裁切边缘；候选横轴必须是一段足够长的连续前景线。
    for y in range(max(2, int(h * 0.15)), min(h - 2, int(h * 0.99))):
        for left, right in _true_runs(mask[y]):
            if right - left + 1 < min_run:
                continue
            heights = _heights_attached_to_axis(mask, y, left, right)
            useful = heights > max(3, int(h * 0.006))
            if int(useful.sum()) < 2:
                continue
            # 竖向信号越多、越高越像谱图横轴；单纯矩形边框的得分很低。
            score = float(np.clip(heights - 3, 0, None).sum())
            if best is None or score > best[0]:
                best = (score, y, left, right, heights)

    if best is None:
        return None
    _, baseline, left, right, heights = best
    prof = np.zeros(w, dtype=np.float32)
    prof[left:right + 1] = heights
    return prof, baseline, left, right


def _find_peaks(prof: np.ndarray, *, min_prominence_ratio: float,
                min_distance: int) -> list[tuple[int, float]]:
    """在高度剖面里找显著峰：非极大抑制 +  prominence 过滤。"""
    if prof.size == 0 or prof.max() <= 0:
        return []
    mx = float(prof.max())

    # 1) 轻度平滑，消掉抗锯齿造成的锯齿
    k = 3
    kernel = np.ones(k, dtype=np.float32) / k
    smooth = np.convolve(prof, kernel, mode="same")

    # 2) 非极大抑制：局部窗口内最大者才可能是峰
    cand: list[tuple[int, float]] = []
    n = smooth.size
    for i in range(n):
        v = smooth[i]
        if v <= 0:
            continue
        lo = max(0, i - min_distance)
        hi = min(n, i + min_distance + 1)
        if v >= smooth[lo:hi].max() - 1e-6:
            # 合并同一平台上的连续列：取中位位置
            j = i
            while j + 1 < n and abs(smooth[j + 1] - v) < 1e-6:
                j += 1
            cand.append(((i + j) // 2, float(prof[max(0, i):min(n, j + 1)].max())))
            i = j  # noqa: B007  （for 循环变量在 Python 中会被覆盖，下面用集合去重）

    # 去重（上面 while 跳跃在 for 里不生效，这里显式去重）
    seen: dict[int, float] = {}
    for x, v in cand:
        if v > seen.get(x, -1):
            seen[x] = v
    cand = sorted(seen.items())

    # 相邻像素列常属于同一根有宽度/抗锯齿的峰线。按高度从高到低做一次真正的
    # 非极大抑制；旧实现试图在 for 循环内修改 i，实际上不会跳过后续列，导致
    # 同一根峰被返回两三次，校准后甚至得到重复 m/z。
    kept: list[tuple[int, float]] = []
    for x, v in sorted(cand, key=lambda item: item[1], reverse=True):
        if all(abs(x - old_x) > min_distance for old_x, _ in kept):
            kept.append((x, v))
    cand = sorted(kept)

    # 3) prominence：峰两侧必须真的"落下来"，滤掉噪声与宽台阶
    out: list[tuple[int, float]] = []
    for idx, (x, v) in enumerate(cand):
        left = cand[idx - 1][0] if idx > 0 else 0
        right = cand[idx + 1][0] if idx + 1 < len(cand) else n - 1
        seg_lo = max(0, left)
        seg_hi = min(n, right + 1)
        if seg_hi - seg_lo < 1:
            seg_hi = n
        local_min = float(smooth[seg_lo:seg_hi].min()) if seg_hi > seg_lo else 0.0
        prominence = v - local_min
        if prominence >= mx * min_prominence_ratio:
            out.append((x, v))
    return out


def extract_peaks_from_image(
    data: bytes,
    *,
    mz_min: float | None = None,
    mz_max: float | None = None,
    min_prominence_ratio: float = 0.02,
    min_peak_distance: int = 3,
    round_mz: bool = True,
) -> ImageExtractionResult:
    """
    从质谱图图片中提取峰。

    :param mz_min/mz_max: 绘图区左右边界对应的 m/z。
        **未提供则只给出相对强度与像素位置，m/z 为 None**——
        本机无 OCR，不猜轴刻度。
    """
    gray = _load_gray(data)
    h, w = gray.shape
    mask = _foreground_mask(gray)
    attached = _axis_attached_profile(mask)
    if attached is not None:
        prof, baseline, left, right = attached
        extraction_mode = "axis_attached"
    else:
        prof, baseline = _column_profile(mask)
        extraction_mode = "global_profile"

    warnings: list[str] = []
    if prof.max() <= 0:
        return ImageExtractionResult(
            peaks=[], image_width=w, image_height=h, plot_left=0, plot_right=w - 1,
            baseline_y=baseline, params={}, warnings=["图像中未检测到任何前景峰，可能不是质谱图"],
            confidence="low")

    # 无明显横轴时才用全部前景列兜底；检测到横轴时保留横轴端点作为 m/z 校准边界。
    if attached is None:
        nz = np.nonzero(prof > 0)[0]
        left, right = int(nz[0]), int(nz[-1])
    if right - left < 10:
        warnings.append("绘图区过窄，可能只截到部分谱图")

    peaks_xy = _find_peaks(prof, min_prominence_ratio=min_prominence_ratio,
                           min_distance=min_peak_distance)

    # 剔除绘图区两端的边界伪影：坐标轴端点常被当成"峰"
    trim = max(2, int((right - left) * 0.01))
    peaks_xy = [(x, v) for x, v in peaks_xy if left + trim <= x <= right - trim]

    if not peaks_xy:
        warnings.append("未找到显著峰，可尝试降低 prominence 阈值")

    mx_height = max(v for _, v in peaks_xy) if peaks_xy else 1.0
    out: list[ExtractedPeak] = []
    span = max(right - left, 1)
    for x, height in peaks_xy:
        rel = height / mx_height * 100.0
        mz = None
        if mz_min is not None and mz_max is not None and mz_max > mz_min:
            frac = (x - left) / span
            mz = mz_min + frac * (mz_max - mz_min)
            if round_mz:
                mz = float(round(mz))
        out.append(ExtractedPeak(mz=mz, x_pixel=int(x),
                                 relative_intensity=round(rel, 2),
                                 raw_height=round(float(height), 2)))

    # 置信度：峰数足够 + 间距规整 ⇒ medium；否则一律 low
    confidence = "low"
    if len(out) >= 5 and mz_min is not None:
        mzs = sorted(p.mz for p in out if p.mz is not None)
        if len(mzs) >= 5:
            gaps = [mzs[i + 1] - mzs[i] for i in range(len(mzs) - 1)]
            med = float(np.median(gaps)) if gaps else 0.0
            if med > 0:
                dev = float(np.mean([abs(g - med) for g in gaps])) / med
                if dev < 0.8:
                    confidence = "medium"
    if mz_min is None:
        warnings.append("未提供 m/z 轴范围：m/z 未解析，仅给出相对强度与像素位置")

    params = {
        "extraction_mode": extraction_mode,
        "min_prominence_ratio": min_prominence_ratio,
        "min_peak_distance": min_peak_distance,
        "mz_min": mz_min,
        "mz_max": mz_max,
        "round_mz": round_mz,
        "plot_span_px": span,
        "baseline_y": baseline,
    }
    return ImageExtractionResult(
        peaks=out, image_width=w, image_height=h, plot_left=left, plot_right=right,
        baseline_y=baseline, params=params, warnings=warnings, confidence=confidence)


def image_is_likely_spectrum(res: ImageExtractionResult) -> bool:
    """粗略判断：像谱图（峰多、绘图区够宽），不像纯文字截图。"""
    return len(res.peaks) >= 3 and (res.plot_right - res.plot_left) > 40
