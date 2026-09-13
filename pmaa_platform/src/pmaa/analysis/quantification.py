"""
定量（M08）。

硬规则：
  * 无校准 → 只能给面积百分比，quantification_level = area_only（A16）；
  * 面积/分子量换算只能标 semiquantitative，且必须写明假设，不是准确 mol%；
  * 检测器/积分方式与校准不一致时禁止输出 calibrated_molar（A21）；
  * 组合峰（共洗脱不可辨识）不强行分摊（A14）；
  * 分母必须写明定义，不能让人误以为覆盖原样所有残基。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..domain.enums import QuantificationLevel, StableErrorCode
from ..domain.models import Quantification

AREA_DENOMINATOR = "已鉴定组分的峰面积之和（未识别峰、低于 LOQ 组分与未覆盖专项糖不计入分母）"
MOLAR_DENOMINATOR = "已鉴定且在校准范围内的 PMAA 总摩尔量"


@dataclass
class Calibration:
    """内标校准模型： y_i = a_i + b_i * x_i ，x = n_i / n_IS。"""
    calibration_id: str
    detector: str
    integration_method: str
    slopes: dict[str, float]
    intercepts: dict[str, float] | None = None
    n_internal_standard: float = 1.0
    valid_batches: list[str] = field(default_factory=list)
    loq_area: float | None = None


def area_percent(areas: dict[str, float]) -> dict[str, float]:
    """无校准时的面积百分比。分母只含已鉴定组分。"""
    total = sum(v for v in areas.values() if v and v > 0)
    if not total:
        return {k: 0.0 for k in areas}
    return {k: (v / total * 100.0 if v and v > 0 else 0.0) for k, v in areas.items()}


def semiquantitative_by_mw(areas: dict[str, float], mw: dict[str, float]) -> dict[str, float]:
    """
    历史"面积/分子量"换算。只能标 semiquantitative，
    并明确写为假设方法——部分文献确用此法，但它不等同响应校准。
    """
    norm = {k: (areas.get(k, 0.0) / mw[k]) for k in areas if mw.get(k)}
    total = sum(norm.values())
    if not total:
        return {k: 0.0 for k in areas}
    return {k: (norm.get(k, 0.0) / total * 100.0) for k in areas}


def calibrate(component_id: str, area: float, area_is: float, cal: Calibration,
              *, batch: str | None = None) -> Quantification:
    """
    由校准曲线反推摩尔量： n_i = n_IS * (y_i - a_i) / b_i。

    等摩尔不同响应的反例（A15）：响应斜率不同时，摩尔比仍应为 50:50。
    """
    if batch and cal.valid_batches and batch not in cal.valid_batches:
        return Quantification(
            mode=QuantificationLevel.area_only, detector=cal.detector,
            integration_method=cal.integration_method, calibration_id=cal.calibration_id,
            value=None, unit=None, denominator_definition=AREA_DENOMINATOR,
        )
    slope = cal.slopes.get(component_id)
    if slope is None or slope == 0:
        return Quantification(
            mode=QuantificationLevel.area_only, detector=cal.detector,
            integration_method=cal.integration_method, calibration_id=cal.calibration_id,
            value=None, unit=None, denominator_definition=AREA_DENOMINATOR,
        )
    intercept = (cal.intercepts or {}).get(component_id, 0.0)
    y = area / area_is if area_is else 0.0
    n = cal.n_internal_standard * (y - intercept) / slope
    below_loq = bool(cal.loq_area is not None and area < cal.loq_area)
    return Quantification(
        mode=QuantificationLevel.calibrated_molar, detector=cal.detector,
        integration_method=cal.integration_method, calibration_id=cal.calibration_id,
        value=round(n, 8), unit="mol_relative", denominator_definition=MOLAR_DENOMINATOR,
        below_loq=below_loq,
    )


def molar_percent(moles: dict[str, float]) -> dict[str, float]:
    total = sum(v for v in moles.values() if v and v > 0)
    if not total:
        return {k: 0.0 for k in moles}
    return {k: (v / total * 100.0 if v and v > 0 else 0.0) for k, v in moles.items()}


def quantification_for(component_id: str, area: float | None, *, calibration: Calibration | None,
                       detector: str, integration_method: str, batch: str | None = None,
                       area_is: float | None = None,
                       unresolved_group_id: str | None = None) -> Quantification:
    """
    统一入口。无校准或检测器/积分方式不一致 → area_only，绝不虚构 mol%。
    """
    if unresolved_group_id:
        return Quantification(
            mode=QuantificationLevel.area_only, detector=detector,
            integration_method=integration_method, calibration_id=None,
            value=None, unit=None, denominator_definition=AREA_DENOMINATOR,
            unresolved_group_id=unresolved_group_id,
        )
    if area is None:
        return Quantification(
            mode=QuantificationLevel.unavailable, detector=detector,
            integration_method=integration_method, calibration_id=None,
            value=None, unit=None, denominator_definition=None,
        )
    if calibration is None:
        return Quantification(
            mode=QuantificationLevel.area_only, detector=detector,
            integration_method=integration_method, calibration_id=None,
            value=None, unit=None, denominator_definition=AREA_DENOMINATOR,
        )
    if calibration.detector != detector or calibration.integration_method != integration_method:
        # A21：不同检测器/积分方式的校准不能混用
        return Quantification(
            mode=QuantificationLevel.area_only, detector=detector,
            integration_method=integration_method, calibration_id=calibration.calibration_id,
            value=None, unit=None, denominator_definition=AREA_DENOMINATOR,
        )
    return calibrate(component_id, area, area_is or 1.0, calibration, batch=batch)
