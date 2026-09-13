"""
样本级 QC 与正式报告门（M08 / D11）。

硬规则：
  * QC 失败 → 冻结正式结构与定量放行，但**保留候选**供研究（A18）；
  * 分装不等于独立制备（A20）；
  * 组成未检出只作软约束，不删候选（A17）；
  * 端基/支化平衡偏差只作调查信号，不为"闭合"篡改峰面积（14.10.2）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..domain.enums import QCStatus, StableErrorCode


@dataclass
class QCCheck:
    code: str
    status: str            # passed / warning / failed
    message: str
    evidence: dict = field(default_factory=dict)


@dataclass
class QCResult:
    status: QCStatus
    checks: list[QCCheck]
    formal_report_allowed: bool
    blockers: list[str]


def _max(statuses: list[str]) -> str:
    for s in ("failed", "warning"):
        if s in statuses:
            return s
    return "passed"


def evaluate_batch_qc(*, blank_peak_ratio: float | None = None,
                      independent_preparations: int = 1,
                      post_methylation_aliquots: int = 1,
                      repeat_cv: float | None = None,
                      recovery: float | None = None,
                      remethylation_shift_exceeds_repeatability: bool = False,
                      unresolved_group_count: int = 0,
                      unidentified_area_ratio: float | None = None,
                      ) -> QCResult:
    checks: list[QCCheck] = []

    # 1) 空白：目标峰低于样本信号 5%
    if blank_peak_ratio is not None:
        st = "passed" if blank_peak_ratio < 0.05 else ("warning" if blank_peak_ratio < 0.10 else "failed")
        checks.append(QCCheck("BLANK_LEVEL", st,
                              f"空白中目标峰占样本信号 {blank_peak_ratio:.1%}（目标 <5%）",
                              {"blank_peak_ratio": blank_peak_ratio}))

    # 2) 独立制备次数（甲基化后分装不计数）
    eff_preps = max(1, independent_preparations)
    checks.append(QCCheck(
        "INDEPENDENT_PREPARATION", "warning" if eff_preps < 3 else "passed",
        f"独立全流程制备 {eff_preps} 次"
        + (f"；甲基化后分装 {post_methylation_aliquots} 份**不计入**独立制备" if post_methylation_aliquots > 1 else ""),
        {"independent_preparations": eff_preps,
         "post_methylation_aliquots": post_methylation_aliquots}))

    # 3) 重复性 CV
    if repeat_cv is not None:
        st = "passed" if repeat_cv <= 0.15 else ("warning" if repeat_cv <= 0.25 else "failed")
        checks.append(QCCheck("REPEATABILITY_CV", st,
                              f"主要组分独立全流程 CV {repeat_cv:.1%}（目标 ≤15%）",
                              {"cv": repeat_cv}))

    # 4) 回收率
    if recovery is not None:
        st = "passed" if 0.8 <= recovery <= 1.2 else "warning"
        checks.append(QCCheck("RECOVERY", st,
                              f"回收率 {recovery:.1%}（目标 80%–120%）", {"recovery": recovery}))

    # 5) 补充甲基化后支化峰系统改变 → 欠甲基化嫌疑
    if remethylation_shift_exceeds_repeatability:
        checks.append(QCCheck(
            "METHYLATION_COMPLETENESS", "warning",
            "补充甲基化后支化候选变化超出已验证重复性：优先怀疑原先欠甲基化，不得直接给支化结论",
            {}))

    # 6) 不可辨识组合峰
    if unresolved_group_count:
        checks.append(QCCheck("UNRESOLVED_GROUP", "warning",
                              f"存在 {unresolved_group_count} 个不可辨识组合峰，不重复计量共享面积", {}))

    # 7) 未识别面积比例
    if unidentified_area_ratio is not None and unidentified_area_ratio > 0.2:
        checks.append(QCCheck("UNIDENTIFIED_AREA", "warning",
                              f"未识别峰面积占比 {unidentified_area_ratio:.1%}，需在报告中单列", {}))

    status = _max([c.status for c in checks]) if checks else "not_assessed"
    qc = QCStatus(status)
    failed = qc == QCStatus.failed
    blockers = [StableErrorCode.QC_FAILED.value] if failed else []
    return QCResult(status=qc, checks=checks,
                    formal_report_allowed=not failed, blockers=blockers)


def composition_consistency(*, ei_sugars: set[str], composition_detected: set[str],
                            composition_detection_limit_known: bool) -> tuple[str, str]:
    """
    单糖组成交叉核验（A17）。返回 (status, message)。
    组成未检出且检出能力未知 → 只给 COMPOSITION_CONFLICT 警告，不删候选。
    """
    extra = sorted(ei_sugars - composition_detected)
    if not extra:
        return "passed", "PMAA 候选糖种均与单糖组成一致"
    if not composition_detection_limit_known:
        return "warning", (f"PMAA 提示 {extra}，但组成分析未检出且检出能力未知："
                           f"保留候选，仅作 {StableErrorCode.COMPOSITION_CONFLICT.value} 提示")
    return "warning", (f"PMAA 提示 {extra}，组成分析在已知检出能力下未检出："
                       f"作为软约束提示，需复核水解回收与组成方法")


def terminal_branch_balance(linkage_counts: dict[int, int], chains: int = 1) -> dict:
    """
    端基—支化检查： n_0 = C + Σ(k-1)*n_k (k≥2)。
    只作调查信号，不要求所有样品严格 1:1，也不为闭合篡改面积（14.10.2）。
    """
    n0 = linkage_counts.get(0, 0)
    expected = chains + sum((k - 1) * cnt for k, cnt in linkage_counts.items() if k >= 2)
    return {
        "observed_terminal": n0,
        "expected_terminal": expected,
        "deviation": n0 - expected,
        "interpretation": "仅作调查信号：低聚物、还原端处理、环链、修饰与欠甲基化都会改变解释",
    }
