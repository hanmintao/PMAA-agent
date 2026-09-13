"""
保留指数（RI）定种通道测试（数据驱动骨架）。

覆盖：
  - load_ri_standards：文件缺失/坏行返回 []（no-op 友好）；正常解析并跳过单条坏数据；
  - match_species_by_ri：matched / ambiguous / nearest / no_hit / no_data 五态；
  - 管线集成：提供 observed RI + 标准 → sugar_species 收窄；无标准 / 无 observed RI 时完全静默（no-op）。
"""
from __future__ import annotations

from pmaa.analysis.pipeline import AnalysisContext, ComponentInput, analyze_component
from pmaa.chemistry.retention_index import (
    RetentionStandard,
    load_ri_standards,
    match_species_by_ri,
)
from pmaa.domain.enums import EvidenceType, SpectrumCompleteness

from _harness import case, eq, ok

METHOD = "HP-5MS_v1"
RI_TABLE = [
    RetentionStandard(METHOD, "Hex", (4, 6), "Glc", retention_index=2050.0),
    RetentionStandard(METHOD, "Hex", (4, 6), "Man", retention_index=2080.0),
    RetentionStandard(METHOD, "Hex", (4, 6), "Gal", retention_index=2110.0),
]
# 宽间距表：用于触发 nearest / no_hit（标准间距 < 2×容差时中点已落精确容差，测不到 nearest）
RI_WIDE = [
    RetentionStandard(METHOD, "Hex", (4, 6), "Glc", retention_index=2050.0),
    RetentionStandard(METHOD, "Hex", (4, 6), "Man", retention_index=2150.0),
]


@case("RI-LOAD-1", "文件缺失/非法时 load_ri_standards 返回 []（no-op 友好）")
def _load_missing():
    import os, tempfile
    eq(load_ri_standards("/no/such/file.json"), [], "缺失文件返回 []")
    p = tempfile.mktemp(suffix=".json")
    with open(p, "w") as f:
        f.write("{not json")
    eq(load_ri_standards(p), [], "坏 JSON 返回 []")
    os.remove(p)


@case("RI-LOAD-2", "正常解析且跳过单条坏数据（不连坐整表）")
def _load_ok():
    import json, os, tempfile
    p = tempfile.mktemp(suffix=".json")
    rows = [s.__dict__ for s in RI_TABLE] + [{"method_id": "X"}]  # 缺字段 → 坏行
    with open(p, "w") as f:
        json.dump(rows, f)
    t = load_ri_standards(p)
    eq(len(t), 3, "坏行被跳过，保留 3 条")
    os.remove(p)


@case("RI-MATCH-1", "observed 2052 精确命中 Glc（Δ=2 ≤ 15）→ matched")
def _match_glc():
    r = match_species_by_ri(2052.0, method_id=METHOD, skeleton="Hex",
                             o_methyl_positions=(4, 6), table=RI_TABLE)
    eq(r.status, "matched", "状态应为 matched")
    eq(r.sugar, "Glc", "定种应为 Glc")
    ok(r.delta is not None and r.delta <= 15, "delta 在精确容差内")


@case("RI-MATCH-2", "两个标准同时落精确容差内 → ambiguous")
def _match_amb():
    tbl = [RetentionStandard(METHOD, "Hex", (4, 6), "Glc", retention_index=2050.0),
           RetentionStandard(METHOD, "Hex", (4, 6), "Man", retention_index=2052.0)]
    r = match_species_by_ri(2051.0, method_id=METHOD, skeleton="Hex",
                             o_methyl_positions=(4, 6), table=tbl)
    eq(r.status, "ambiguous", "状态应为 ambiguous")
    ok(len(r.candidates) >= 2, "应列出多个候选")


@case("RI-MATCH-3", "无精确命中但最近邻落放宽容差 → nearest（告警）")
def _match_near():
    r = match_species_by_ri(2075.0, method_id=METHOD, skeleton="Hex",
                             o_methyl_positions=(4, 6), table=RI_WIDE)
    eq(r.status, "nearest", "状态应为 nearest")
    ok(r.delta is not None and 15 < r.delta <= 30, "delta 在放宽容差内")


@case("RI-MATCH-4", "全超出放宽容差 → no_hit")
def _match_nohit():
    r = match_species_by_ri(2200.0, method_id=METHOD, skeleton="Hex",
                             o_methyl_positions=(4, 6), table=RI_WIDE)
    eq(r.status, "no_hit", "状态应为 no_hit")


@case("RI-MATCH-5", "表中无该 (方法,骨架,甲基位点) → no_data（静默）")
def _match_nodata():
    r = match_species_by_ri(2050.0, method_id="OTHER_METHOD", skeleton="Hex",
                             o_methyl_positions=(4, 6), table=RI_TABLE)
    eq(r.status, "no_data", "状态应为 no_data")


# ----------------------------------------------------------------- 管线集成
def _acc_input():
    ions = {59: "+", 71: "+", 86: "+", 101: "+", 112: "+", 129: "red", 143: "+",
            161: "+", 172: "+", 188: "+", 202: "+", 218: "+", 232: "+", 245: "+"}
    return ComponentInput(component_id="ri_acc", evidence_type=EvidenceType.qualitative_ion_list,
                           ions=ions, spectrum_completeness=SpectrumCompleteness.full_acquired_range,
                           provenance="observed", retention_index=2052.0)


def _acc_ctx():
    return AnalysisContext(analysis_id="ri_acc_a", sample_reduction_label="NaBD4",
                           method_id=METHOD, composition_detected={"Gal"}, top_k=6)


@case("RI-INT-1", "提供 observed RI + 标准 → 糖种收窄到 Glc（RI 定种通道生效）")
def _int_matched():
    import pmaa.analysis.pipeline as P
    orig = P.load_ri_standards
    P.load_ri_standards = lambda *a, **k: list(RI_TABLE)
    try:
        res = analyze_component(_acc_input(), _acc_ctx())
        eq(res.sugar_species, "Glc", "RI 定种应为 Glc")
        ok(res.species_evidence and res.species_evidence.get("channel") == "RI", "应有 RI 证据")
        ok(any("保留指数定种" in n for n in res.notes), "应记录 RI 定种")
        ok("RI 定种" in res.interpretation.residue_linkage.text, "三段式第2段应体现 RI 定种")
    finally:
        P.load_ri_standards = orig


@case("RI-INT-2", "无标准 / 无 observed RI → 通道完全静默（no-op），sugar_species 为 None")
def _int_noop():
    # 未提供 observed RI → 不应改写任何结论
    res = analyze_component(
        ComponentInput(component_id="ri_noop", evidence_type=EvidenceType.qualitative_ion_list,
                       ions=_acc_input().ions,
                       spectrum_completeness=SpectrumCompleteness.full_acquired_range,
                       provenance="observed"),
        _acc_ctx())
    eq(res.sugar_species, None, "无 observed RI 时 sugar_species 应为 None")
    # 有 observed RI 但标准表为空（load 返回 []）→ 同样静默
    import pmaa.analysis.pipeline as P
    orig = P.load_ri_standards
    P.load_ri_standards = lambda *a, **k: []
    try:
        res2 = analyze_component(_acc_input(), _acc_ctx())
        eq(res2.sugar_species, None, "标准表为空时 sugar_species 应为 None")
    finally:
        P.load_ri_standards = orig
