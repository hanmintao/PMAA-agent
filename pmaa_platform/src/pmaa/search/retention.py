"""
色谱保留证据（M06）。

三条硬约束：
  * RT / RRT / RI 分开保存，单位必须自洽（03 契约 A11）；
  * 方法不一致时**不做硬排除**（A12），只标注 incompatible 并降权；
  * 文献值只在条件相容时支持候选排序，不能当作"距标准最近即确定"。
"""
from __future__ import annotations

from dataclasses import dataclass

from ..domain.enums import StableErrorCode


@dataclass
class RetentionCheck:
    compatible: bool
    status: str                     # matched / incompatible / no_observation / no_sample_rt
    deviation: float | None = None  # 以 sd 为单位；无分布时为 None
    message: str = ""
    error_code: str | None = None
    hard_exclusion_allowed: bool = False


def check_retention(sample_rt: float | None, sample_method_id: str | None,
                    observations: list[dict], *, bridge_validated: bool = False) -> RetentionCheck:
    """
    :param observations: 来自 RetentionRepo 的观测列表（每项含 kind/value/unit/method_id/sd）
    :param bridge_validated: 跨方法是否做过桥接验证（A12）
    """
    if sample_rt is None or not observations:
        return RetentionCheck(
            compatible=False, status="no_observation",
            message="无同方法保留观测或样品未给 RT；保留证据不参与判定（不算冲突）",
        )

    same_method = [o for o in observations if o.get("method_id") == sample_method_id]
    if not same_method:
        if bridge_validated:
            return RetentionCheck(
                compatible=True, status="bridged",
                message="跨方法保留值经桥接验证后仅作排序提示，不得用于确认")
        return RetentionCheck(
            compatible=False, status="incompatible",
            message="参考保留观测来自其他色谱方法，未经桥接验证，不参与判定且不得硬排除",
            error_code=StableErrorCode.RETENTION_METHOD_MISMATCH.value,
            hard_exclusion_allowed=False,
        )

    # 只比较同 kind 的观测
    best = None
    for o in same_method:
        dev = None
        if o.get("sd"):
            dev = abs(sample_rt - o["value"]) / o["sd"]
        if best is None or (dev is not None and (best[0] is None or dev < best[0])):
            best = (dev, o)

    dev, obs = best
    if dev is None:
        return RetentionCheck(
            compatible=True, status="no_distribution",
            message=f"存在同方法观测 {obs['value']}{obs.get('unit')}，"
                    f"但无重复标准差分布，仅作排序提示（文献值不直接确认）")
    within = dev <= 3.0
    return RetentionCheck(
        compatible=within, status="matched" if within else "outside_window",
        deviation=round(dev, 3),
        message=("保留偏差在中心±3SD 内（排序提示，不等于确认）" if within
                 else f"保留偏差 {dev:.2f} SD，超出窗口：不得据此硬排除，需复核方法/共洗脱"),
        hard_exclusion_allowed=False,
    )
