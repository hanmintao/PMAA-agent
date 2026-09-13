"""
数值谱匹配（M05，numeric_spectrum 模式）。

采用质谱检索常用的 m/z 与强度双加权余弦，并同时计算**反向分数**：
  forward  = cos(query, reference)   参考谱是否解释了查询
  reverse  = cos(reference, query)   惩罚"参考峰远多于查询"的虚假高分
最终分取两者的调和平均，避免大谱Reference靠"覆盖"刷分。

只输出匹配分数，**不输出正确概率**（03 契约 A13 / 计划第 2 节第 7 条）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from ..config import SEARCH_DEFAULTS


@dataclass
class NumericMatch:
    record_id: str
    score: float
    forward: float
    reverse: float
    matched_mz: list[float]
    ion_ratio_checks: list[dict]


def _weighted(spectrum: dict[float, float]) -> dict[float, float]:
    a = SEARCH_DEFAULTS["numeric_mz_exponent"]
    b = SEARCH_DEFAULTS["numeric_int_exponent"]
    # 强度按基峰归一到 100，避免绝对响应差异影响相似度
    base = max(spectrum.values()) if spectrum else 1.0
    base = base or 1.0
    return {mz: (mz ** a) * ((intensity / base * 100.0) ** b) for mz, intensity in spectrum.items()}


def _cosine(q: dict[float, float], r: dict[float, float]) -> tuple[float, list[float]]:
    common = sorted(set(q) & set(r))
    num = sum(q[m] * r[m] for m in common)
    nq = math.sqrt(sum(v * v for v in q.values())) or 1.0
    nr = math.sqrt(sum(v * v for v in r.values())) or 1.0
    return num / (nq * nr), common


def match_numeric(query: dict[float, float], references: dict[str, dict[float, float]],
                  top_k: int | None = None,
                  ion_ratio_pairs: list[tuple[float, float]] | None = None) -> list[NumericMatch]:
    """
    :param query: {mz: intensity}
    :param references: {record_id: {mz: intensity}}
    :param ion_ratio_pairs: 需要检验的鉴别离子对 (mz_a, mz_b)，用于离子比检验
    """
    qw = _weighted(query)
    results: list[NumericMatch] = []
    for rid, ref in references.items():
        rw = _weighted(ref)
        forward, common = _cosine(qw, rw)
        reverse, _ = _cosine(rw, qw)
        score = 0.0
        if forward > 0 and reverse > 0:
            score = 2 * forward * reverse / (forward + reverse)   # 调和平均
        checks = []
        for a, b in (ion_ratio_pairs or []):
            qa, qb = query.get(a), query.get(b)
            ra, rb = ref.get(a), ref.get(b)
            if qa and qb and ra and rb:
                checks.append({
                    "pair": [a, b],
                    "query_ratio": round(qa / qb, 4),
                    "reference_ratio": round(ra / rb, 4),
                    "consistent": abs((qa / qb) - (ra / rb)) / max(ra / rb, 1e-9) <= 0.5,
                })
        results.append(NumericMatch(record_id=rid, score=round(score, 6),
                                    forward=round(forward, 6), reverse=round(reverse, 6),
                                    matched_mz=common, ion_ratio_checks=checks))
    results.sort(key=lambda m: (-m.score, m.record_id))
    return results[: top_k or SEARCH_DEFAULTS["top_k"]]


def normalize_spectrum(spectrum: dict[float, float], *, base_peak: float | None = None) -> dict[float, float]:
    """按基峰归一到 100；空谱原样返回（不填 0，不填 100）。"""
    if not spectrum:
        return {}
    base = base_peak or max(spectrum.values())
    if not base:
        return dict(spectrum)
    return {mz: intensity / base * 100.0 for mz, intensity in spectrum.items()}
