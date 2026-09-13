"""
评分公平性与参考库完整性回归（修改方案 P1/P2/P3）。

覆盖：
  SCO-COV-1  coverage 恒 ∈ [0,1]（P1 修复 boost 基准不一致；原 bug 4-Glc=1.565）
  SCO-COV-2  完整谱图 → 正确结构 4-O-甲基己糖（2,3,6-连接）排第 1
  SCO-COV-3  漏录弱峰(262) 时 4-O-甲基己糖仍给出候选且标低覆盖，行为可解释不崩溃
  SCO-LOW-1  单取代己糖（参考离子少）候选标 low_coverage_warning=True
  SCO-F1-1   评分模式切到 f1 时，正确结构仍排第 1（对称评分不回归）
"""
from __future__ import annotations

from pmaa.analysis.pipeline import ComponentInput, AnalysisContext, analyze_component
from pmaa.domain.enums import EvidenceType, SpectrumCompleteness, QCStatus
from pmaa.search.engine import load_library
from pmaa.search.qualitative import QualitativeIndex
from pmaa.config import SEARCH_DEFAULTS

from _harness import case, ok, eq


FULL_PEAKS = [59, 71, 87, 100, 116, 129, 143, 160, 171, 189, 202, 218, 231, 245, 262]


def _ctx(top_k=12):
    return AnalysisContext(analysis_id="t", method_id=None, top_k=top_k,
                           sample_reduction_label="NaBD4", qc_status=QCStatus.not_assessed)


def _qual_comp(peaks, completeness=SpectrumCompleteness.selected_ions):
    return ComponentInput(component_id="c", evidence_type=EvidenceType.qualitative_ion_list,
                           ions={float(m): "red" for m in peaks},
                           spectrum_completeness=completeness, provenance="observed")


@case("SCO-COV-1", "coverage 恒 ∈ [0,1]（P1 修复 boost 基准不一致）")
def test_coverage_bounded():
    # 直接扫全库：任意查询/条目 coverage 不得 >1
    _, by_id = load_library()
    idx = QualitativeIndex([__e for __e in (
        type("E", (), {"record_id": rid, "ions": e.ions, "meta": e.meta})()
        for rid, e in by_id.items())])
    q = {float(m): __import__("pmaa.domain.enums", fromlist=["IntensityClass"]).IntensityClass.reported_gt20pct
         for m in FULL_PEAKS}
    worst = 0.0
    for rid, e in by_id.items():
        sb = idx.score(q, type("E", (), {"record_id": rid, "ions": e.ions, "meta": e.meta})())
        worst = max(worst, sb.coverage)
    ok(worst <= 1.0, f"coverage 上限应为 ≤1，实际 {worst}")
    # 流水线候选同样约束
    res = analyze_component(_qual_comp(FULL_PEAKS), _ctx())
    for c in res.candidate_derivatives:
        ok(c.coverage is None or 0 <= c.coverage <= 1.0, f"{c.derivative_id} coverage={c.coverage}")


@case("SCO-COV-2", "完整谱图 → 正确结构 4-O-甲基己糖（2,3,6-连接）排第 1")
def test_correct_structure_top1():
    res = analyze_component(_qual_comp(FULL_PEAKS), _ctx())
    ok(res.candidate_derivatives, "应有候选")
    top = res.candidate_derivatives[0]
    eq(top.derivative_id, "Hex|Me4|Ac1.2.3.5.6|NaBD4_CH3I",
        f"Top1 应为 4-O-甲基己糖（2,3,6-连接），实际 {top.derivative_id}")
    # coverage 应为诚实的 1.0（该条目离子全命中）
    eq(top.coverage, 1.0)


@case("SCO-COV-3", "漏录弱峰(262) 时正确结构仍给候选且标低覆盖，不崩溃")
def test_omit_weak_peak_explainable():
    res = analyze_component(_qual_comp([p for p in FULL_PEAKS if p != 262]), _ctx())
    ids = [c.derivative_id for c in res.candidate_derivatives]
    ok(any(i.startswith("Hex|Me4") for i in ids), "漏 262 后 4-O-甲基己糖仍应在候选中")
    # 该稀疏条目应被标低覆盖，提示用户补全上传
    low = [c for c in res.candidate_derivatives if c.derivative_id.startswith("Hex|Me4")]
    ok(low and low[0].low_coverage_warning, "稀疏条目应标 low_coverage_warning")
    ok(any("参考条目稀疏" in n for n in res.notes), "应出现参考条目稀疏说明")


@case("SCO-LOW-1", "单取代己糖（参考离子少）候选标 low_coverage_warning=True")
def test_low_coverage_flag():
    res = analyze_component(_qual_comp(FULL_PEAKS), _ctx())
    flags = [c for c in res.candidate_derivatives if c.low_coverage_warning]
    ok(flags, "应至少有一个低覆盖候选（单取代己糖离子少）")
    # 低覆盖候选的参考离子数应 < 阈值
    thr = SEARCH_DEFAULTS["low_coverage_ref_ion_threshold"]
    ok(all((c.score_components.get("reference_ion_count") or 0) < thr for c in flags),
       f"参考稀疏候选的收录离子数应 < 阈值 {thr}")


@case("SCO-F1-1", "评分模式切 f1 时正确结构仍排第 1（对称评分不回归）")
def test_f1_mode_still_correct():
    prev = SEARCH_DEFAULTS.get("scoring_mode")
    SEARCH_DEFAULTS["scoring_mode"] = "f1"
    try:
        res = analyze_component(_qual_comp(FULL_PEAKS), _ctx())
        ok(res.candidate_derivatives, "f1 模式应有候选")
        eq(res.candidate_derivatives[0].derivative_id,
            "Hex|Me4|Ac1.2.3.5.6|NaBD4_CH3I",
            f"f1 模式 Top1 仍应为 4-O-甲基己糖，实际 {res.candidate_derivatives[0].derivative_id}")
    finally:
        SEARCH_DEFAULTS["scoring_mode"] = prev


@case("SCO-SUGAR-1", "参考库只收录一个糖种时不得把库覆盖误当成正交定种")
def test_single_library_sugar_is_not_confirmed_species():
    res = analyze_component(_qual_comp(FULL_PEAKS), _ctx())
    text = res.interpretation.residue_linkage.text
    ok("仅参考库命中" in text and "库覆盖不足" in text,
       f"单一库命中必须带覆盖不足警告，实际：{text}")
    ok("SUGAR_SPECIES_EVIDENCE" in res.missing_evidence,
       "应要求单糖组成/RI/标准品定种证据")
