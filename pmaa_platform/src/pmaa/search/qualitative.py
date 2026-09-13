"""
定性离子检索（M05，qualitative_ion_list 模式）。

严格遵守 14.7.3：
  * 不把 "+" 变成强度；
  * 不调用全谱余弦；
  * 未列出的离子不等于未检出（A02）——因此只做**软性排序**，绝不做硬排除；
  * 权重按离子区分力（在该库中的稀有度）加权，稀有离子更有鉴别力；
  * 不使用统一的 ">20% 才保留" 阈值（14.7.1 末：234 在 19.6% 仍有价值）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..config import SEARCH_DEFAULTS
from ..domain.enums import IntensityClass


@dataclass
class RefEntry:
    record_id: str
    ions: dict[float, IntensityClass]
    meta: dict = field(default_factory=dict)


@dataclass
class ScoreBreakdown:
    record_id: str
    score: float
    coverage: float          # 参考谱被查询解释的比例
    precision: float         # 查询谱被参考解释的比例
    matched_mz: list[float]
    ref_only_mz: list[float]
    query_only_mz: list[float]
    red_matched: int
    red_total: int
    ref_ion_count: int = 0          # 参考条目收录离子数（P3：低覆盖判定用）
    low_coverage: bool = False      # 参考条目离子数 < 阈值，排名对上传完整度敏感（P3）
    signature_recall: float = 1.0   # 兼容旧字段名：实为“参考强峰召回”，不是唯一特征离子
    signature_matched: int = 0      # 命中的参考强峰数
    signature_total: int = 0        # 参考条目的强峰总数
    missing_strong_penalty_applied: bool = False


class QualitativeIndex:
    """对参考库建立 IDF 权重，然后对定性查询打分。"""

    def __init__(self, entries: list[RefEntry]):
        self.entries = entries
        self.n = len(entries)
        df: dict[float, int] = {}
        for e in entries:
            for mz in e.ions:
                df[mz] = df.get(mz, 0) + 1
        cap = SEARCH_DEFAULTS["qualitative_idf_cap"]
        # idf = ln(N / df)，上限封顶，避免单个稀有离子主导排序
        self.weight: dict[float, float] = {
            mz: min(cap, math.log((self.n + 1) / (c + 0.5))) for mz, c in df.items()
        }
        self.default_weight = cap

    def w(self, mz: float) -> float:
        return self.weight.get(mz, self.default_weight)

    def score(self, query: dict[float, IntensityClass], entry: RefEntry, *,
              allow_missing_reference_penalty: bool = False) -> ScoreBreakdown:
        boost = SEARCH_DEFAULTS["qualitative_red_boost"]
        ref_ions = entry.ions
        matched = sorted(set(query) & set(ref_ions))

        def _w(mz: float, cls_map: dict[float, IntensityClass]) -> float:
            base = self.w(mz)
            # 红色 = 原表记载 >20%，是强信号，给予增益；但绝不当作数值强度
            return base * (boost if cls_map.get(mz) == IntensityClass.reported_gt20pct else 1.0)

        ref_total = sum(_w(mz, ref_ions) for mz in ref_ions) or 1.0
        q_total = sum(_w(mz, query) for mz in query) or 1.0
        # 命中离子按"查询侧红标（基峰/强峰）"额外增益：precision（查询谱被参考解释）用查询侧 boost，自洽。
        matched_w = sum(self.w(mz) * (boost if query.get(mz) == IntensityClass.reported_gt20pct else 1.0)
                        for mz in matched)
        # coverage（参考谱被查询解释）必须用【参考侧】boost 与 ref_total 同源，
        # 否则分子查询侧 / 分母参考侧基准不一致会算到 >1（如 4-Glc=1.565），
        # 虚高低离子条目、并掩盖"漏录峰"导致的排名漂移（修改方案 P1）。
        matched_ref_w = sum(self.w(mz) * (boost if ref_ions.get(mz) == IntensityClass.reported_gt20pct else 1.0)
                            for mz in matched)

        precision = matched_w / q_total           # ∈[0,1]，查询侧自洽
        coverage = matched_ref_w / ref_total      # ∈[0,1]，参考侧自洽

        a = SEARCH_DEFAULTS["qualitative_recall_weight"]
        mode = SEARCH_DEFAULTS.get("scoring_mode", "geometric")
        if mode == "f1":
            # 对称 F1：precision 与 coverage 同等对待，库离子收录多少不主导排名（P2 可选）
            denom = precision + coverage
            score = (2.0 * precision * coverage / denom) if denom > 0 else 0.0
        else:
            # 加权几何均值（默认）：precision 权重更高，避免小分母刷分；两侧只排序不做硬阈值。
            score = (max(precision, 1e-9) ** a) * (max(coverage, 1e-9) ** (1.0 - a))

        # ----------------------------------------------------- 参考强峰召回（条件化负证据）
        # 原表红色只表示相对强度 >20%，不是“该离子只属于此结构”的 signature。
        # selected_ions 或图片/文档峰表里没列出某离子，不代表实验上未检出（A02），
        # 因而这时只展示召回率，不能因缺失而扣分。只有调用方明确确认“仪器直接导出的
        # 完整峰表”时，才允许将未见参考强峰作为小幅软降权，且永不硬排除候选。
        signature_ions = [mz for mz, c in ref_ions.items()
                          if c == IntensityClass.reported_gt20pct]
        sig_total = len(signature_ions)
        sig_matched = sum(1 for mz in signature_ions if mz in query)
        signature_recall = (sig_matched / sig_total) if sig_total else 1.0
        if allow_missing_reference_penalty:
            gamma = SEARCH_DEFAULTS["qualitative_missing_strong_penalty"]
            score = score * (1.0 - gamma * (1.0 - signature_recall))

        red_total = sum(1 for mz, c in ref_ions.items() if c == IntensityClass.reported_gt20pct)
        red_matched = sum(1 for mz in matched if ref_ions.get(mz) == IntensityClass.reported_gt20pct)
        ref_ion_count = len(ref_ions)
        low_coverage = ref_ion_count < SEARCH_DEFAULTS["low_coverage_ref_ion_threshold"]

        return ScoreBreakdown(
            record_id=entry.record_id, score=round(score, 6),
            coverage=round(coverage, 6), precision=round(precision, 6),
            matched_mz=matched,
            ref_only_mz=sorted(set(ref_ions) - set(query)),
            query_only_mz=sorted(set(query) - set(ref_ions)),
            red_matched=red_matched, red_total=red_total,
            ref_ion_count=ref_ion_count, low_coverage=low_coverage,
            signature_recall=round(signature_recall, 6),
            signature_matched=sig_matched, signature_total=sig_total,
            missing_strong_penalty_applied=allow_missing_reference_penalty,
        )

    def search(self, query: dict[float, IntensityClass], top_k: int | None = None, *,
               allow_missing_reference_penalty: bool = False,
               ) -> list[ScoreBreakdown]:
        out = [self.score(query, e,
                          allow_missing_reference_penalty=allow_missing_reference_penalty)
               for e in self.entries]
        out.sort(key=lambda s: (-s.score, s.record_id))
        return out[: top_k or SEARCH_DEFAULTS["top_k"]]


def group_near_ties(scores: list[ScoreBreakdown], delta: float | None = None) -> list[list[str]]:
    """
    把分数接近的候选归为并列组（A13：0.93 与 0.929 必须并列，不能只留 Top1）。
    返回按分数降序排列的并列组（组内为 record_id）。
    """
    if not scores:
        return []
    d = delta if delta is not None else SEARCH_DEFAULTS["near_tie_delta"]
    groups: list[list[str]] = [[scores[0].record_id]]
    ref = scores[0].score
    for s in scores[1:]:
        if ref - s.score <= d:
            groups[-1].append(s.record_id)
        else:
            groups.append([s.record_id])
            ref = s.score
    return groups
