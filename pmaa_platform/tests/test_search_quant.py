"""
保留证据、匹配分数与定量验收（A11–A16、A21、A24）。
"""
from __future__ import annotations

from pmaa.analysis.quantification import (
    Calibration,
    area_percent,
    molar_percent,
    quantification_for,
    semiquantitative_by_mw,
)
from pmaa.domain.enums import StableErrorCode
from pmaa.domain.models import RetentionObservation
from pmaa.search.qualitative import QualitativeIndex, RefEntry, group_near_ties
from pmaa.search.retention import check_retention
from pmaa.domain.enums import IntensityClass

from _harness import case, eq, ok


@case("A11", "RRT 量纲错误：单位必须为 dimensionless")
def _a11():
    try:
        RetentionObservation(kind="RRT", value=0.737, unit="min", method_id="m1")
    except Exception as e:
        ok("RRT_UNIT_MISMATCH" in str(e), f"应报量纲错误，实际 {e}")
    else:
        raise AssertionError("RRT 带 min 单位竟然通过校验")
    o = RetentionObservation(kind="RRT", value=0.737, unit="dimensionless",
                             method_id="m1", reference_identity="myo-inositol hexaacetate")
    eq(o.unit, "dimensionless")


@case("A12", "跨柱保留不直接比较，且不得硬排除")
def _a12():
    obs = [{"method_id": "HP5ms_B", "kind": "RRT", "value": 0.759, "unit": "dimensionless", "sd": 0.01}]
    r = check_retention(23.4, "SP2380_A", obs, bridge_validated=False)
    eq(r.status, "incompatible", "跨方法应判不兼容")
    eq(r.error_code, StableErrorCode.RETENTION_METHOD_MISMATCH.value, "错误码")
    eq(r.hard_exclusion_allowed, False, "不得硬排除")
    r2 = check_retention(23.4, "SP2380_A", obs, bridge_validated=True)
    eq(r2.status, "bridged", "桥接验证后仅作排序提示")
    eq(r2.hard_exclusion_allowed, False, "桥接后仍不得硬排除")


@case("A13", "最高分不是概率：近邻并列必须保留")
def _a13():
    idx = QualitativeIndex([
        RefEntry("A", {118.0: IntensityClass.reported_present, 129.0: IntensityClass.reported_present}),
        RefEntry("B", {118.0: IntensityClass.reported_present}),
    ])
    hits = idx.search({118.0: IntensityClass.reported_present, 129.0: IntensityClass.reported_present})
    # 构造 0.93 / 0.929 的近邻并列
    class S:
        def __init__(s, rid, sc): s.record_id, s.score = rid, sc
    groups = group_near_ties([S("A", 0.93), S("B", 0.929)], delta=0.02)
    eq(len(groups), 1, "0.93 与 0.929 必须同组")
    eq(groups[0], ["A", "B"], "并列组内容")
    groups2 = group_near_ties([S("A", 0.93), S("B", 0.80)], delta=0.02)
    eq(len(groups2), 2, "分差大时不应并列")


@case("A13b", "库外或极低匹配不得把相对 Top1 伪装成可靠候选")
def _a13b():
    from pmaa.analysis.pipeline import AnalysisContext, ComponentInput, analyze_component
    from pmaa.domain.enums import DerivativeIdentityLevel, EvidenceType

    result = analyze_component(
        ComponentInput(component_id="out_of_library", evidence_type=EvidenceType.qualitative_ion_list,
                       ions={777.0: "+"}),
        AnalysisContext(sample_reduction_label="NaBD4"),
    )
    eq(result.derivative_identity_level, DerivativeIdentityLevel.unknown,
       "极低匹配只能记为 unknown")
    ok("LIBRARY_COVERAGE_LOW" in result.missing_evidence, "必须提示参考库覆盖不足")
    ok(any(x.get("code") == StableErrorCode.LOW_LIBRARY_MATCH.value for x in result.conflicts),
       "必须返回稳定的低匹配警告码")


@case("A14", "相同 EI 共洗脱无法强拆：保持组合峰，不重复计量")
def _a14():
    q = quantification_for("p1", 1000.0, calibration=None, detector="EI_TIC",
                           integration_method="tic", unresolved_group_id="grp_1")
    eq(q.mode.value, "area_only", "组合峰不得校准定量")
    eq(q.unresolved_group_id, "grp_1", "组合峰 ID 必须保留")
    eq(q.value, None, "不得分摊出摩尔量")


@case("A15", "等摩尔不同检测响应 → 摩尔比 50:50")
def _a15():
    cal = Calibration(calibration_id="c1", detector="EI_TIC", integration_method="tic",
                      slopes={"p1": 1.0, "p2": 2.0}, intercepts={"p1": 0.0, "p2": 0.0},
                      n_internal_standard=1.0)
    moles = {}
    for pid, area in (("p1", 100.0), ("p2", 200.0)):
        q = quantification_for(pid, area, calibration=cal, detector="EI_TIC",
                               integration_method="tic", area_is=1.0)
        eq(q.mode.value, "calibrated_molar", f"{pid} 应为校准摩尔量")
        moles[pid] = q.value
    pct = molar_percent(moles)
    eq(round(pct["p1"], 6), 50.0, "摩尔占比")
    eq(round(pct["p2"], 6), 50.0, "摩尔占比")


@case("A16", "无校准不报准确 mol%：只能给面积百分比")
def _a16():
    ap = area_percent({"p1": 100.0, "p2": 200.0})
    eq(round(ap["p1"], 6), 33.333333, "面积占比")
    eq(round(ap["p2"], 6), 66.666667, "面积占比")
    q = quantification_for("p1", 100.0, calibration=None, detector="EI_TIC", integration_method="tic")
    eq(q.mode.value, "area_only", "定量等级")
    ok("面积" in (q.denominator_definition or ""), "分母定义必须写明")
    # 面积/分子量只能标 semiquantitative
    sq = semiquantitative_by_mw({"p1": 100.0, "p2": 200.0}, {"p1": 100.0, "p2": 200.0})
    eq(round(sq["p1"], 6), 50.0, "半定量换算")
    ok(q.mode != "calibrated_molar", "不得使用 calibrated_molar 标签")


@case("A21", "不同检测器/积分方式的校准不得混用")
def _a21():
    cal = Calibration(calibration_id="fid_cal", detector="FID", integration_method="tic",
                      slopes={"p1": 1.0}, intercepts={"p1": 0.0})
    q = quantification_for("p1", 100.0, calibration=cal, detector="EI_TIC", integration_method="tic")
    eq(q.mode.value, "area_only", "检测器不一致时不得输出校准摩尔量")
    # 积分方式不一致同样拒绝
    cal2 = Calibration(calibration_id="c2", detector="EI_TIC", integration_method="eic",
                       slopes={"p1": 1.0}, intercepts={"p1": 0.0})
    q2 = quantification_for("p1", 100.0, calibration=cal2, detector="EI_TIC", integration_method="tic")
    eq(q2.mode.value, "area_only", "积分方式不一致时不得输出校准摩尔量")


@case("A24", "真实标准也不得抹去原残基歧义")
def _a24():
    from pmaa.chemistry.mapping import map_from_derivative
    origins = map_from_derivative("Hex", [2, 3, 6], [1, 4, 5])
    eq(len(origins), 2, "2,3,6-Me3/1,4,5-Ac3 必须保留两个来源")
    rings = {o.framework.ring_form for o in origins}
    eq(rings, {"p", "f"}, "吡喃/呋喃两个来源都要保留")
