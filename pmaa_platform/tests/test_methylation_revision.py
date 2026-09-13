"""
甲基化判读模块修改方案回归测试（修改方案 P1–P7 / 4.3–4.7）。

验收基准案例：4,6-二-O-甲基己糖（真实结构 = 2,3-连接的 D-吡喃半乳糖残基）。
谱图高质量区 245/232/218 对应 M−15/M−28/M−42，估计 M≈260（己糖 PMAA）。

断言：
  - 不再用固定骨架 MW 启发式硬剔除；仅由已验证完整的单糖组成执行硬约束；
  - 强度加权召回近似（P2）+ 门控后排名可靠；
  - 候选过滤（P3）保留 Hex 候选、剔除不物理者；
  - 糖种/环型不写死（P5）：糖组含 Gal 但不落单一糖名，环型 unknown；
  - 证据分级（P6）：整体置信度为“候选”，不得出现“确认”；
  - 三段式输出（P7）：第3段恒为 gated 未确认。
"""
from __future__ import annotations

from pmaa.analysis.pipeline import AnalysisContext, ComponentInput, analyze_component
from pmaa.chemistry.molecular_weight import (
    SKELETON_MW,
    allowed_skeletons,
    estimate_molecular_weight,
    gate_skeleton,
)
from pmaa.data import repositories as repo
from pmaa.domain.enums import EvidenceType, SpectrumCompleteness
from pmaa.domain.models import DerivativeIdentityLevel

from _harness import EI_TABLE, case, eq, ok


def _ensure_refs():
    """保证参考库已灌入（测试 DB 为空时从 bundled EI 表提交）。"""
    if repo.ReferenceRepo.list_all(include_quarantined=True):
        return
    from pmaa.ingest.importer import EISummaryTableImporter
    if EI_TABLE:
        EISummaryTableImporter(source_title="test_bundled").commit(EI_TABLE)


# ----------------------------------------------------------------- 分子量门控单元
@case("REV-MW-1", "历史高质量离子簇启发式仍可重放，但不进入生产判定")
def _mw_estimate():
    m = estimate_molecular_weight([59, 71, 86, 101, 112, 129, 143, 161, 172, 188, 202, 218, 232, 245])
    eq(m, 260, "M 估计值")
    # 缺高质量区信息时不强行门控
    eq(estimate_molecular_weight([59, 71, 86, 101, 129]), None, "信息不足应返回 None")


@case("REV-MW-2", "历史固定MW工具保持兼容，仅供方法研究")
def _mw_allowed():
    eq(allowed_skeletons(260, "NaBD4", 2.0), ["Hex"], "M=260 → 仅 Hex")
    eq(allowed_skeletons(232, "NaBD4", 2.0), ["dHex"], "M=232 → 仅 dHex")
    eq(allowed_skeletons(204, "NaBD4", 2.0), ["Pen"], "M=204 → 仅 Pen")
    ok(gate_skeleton("dHex", 260, "NaBD4", 2.0) is False, "dHex 在 M=260 不通")
    ok(gate_skeleton("Hex", 260, "NaBD4", 2.0) is True, "Hex 在 M=260 通过")
    # 未知骨架 / 无 M 估计 → 放行（不为缺失信息误杀，尊重 A02）
    ok(gate_skeleton("Hex", None, "NaBD4", 2.0) is True, "M 未知时放行")
    ok(gate_skeleton(None, 260, "NaBD4", 2.0) is True, "骨架未知时放行")


# ----------------------------------------------------------------- 验收案例集成
@case("REV-ACC-1", "组成列表经验证完整时：4,6-Hex 保留，dHex/Pen 按组成约束剔除")
def _acceptance_top1():
    _ensure_refs()
    ions = {59: "+", 71: "+", 86: "+", 101: "+", 112: "+", 129: "red", 143: "+",
            161: "+", 172: "+", 188: "+", 202: "+", 218: "+", 232: "+", 245: "+"}
    res = analyze_component(
        ComponentInput(component_id="acc_46gal", evidence_type=EvidenceType.qualitative_ion_list,
                       ions=ions, spectrum_completeness=SpectrumCompleteness.full_acquired_range,
                       provenance="observed"),
        AnalysisContext(analysis_id="acc", sample_reduction_label="NaBD4",
                        composition_detected={"Gal"},
                        composition_detection_limit_known=True, top_k=6),
    )
    ok(len(res.candidate_derivatives) > 0, "应有候选")
    top = res.candidate_derivatives[0]
    eq(top.o_methyl_positions, [4, 6], "Top1 必须是 4,6-二-O-甲基")
    ok(top.derivative_id.startswith("Hex"), "Top1 必须是己糖骨架")
    # 门控后不得出现 dHex / Pen 候选（不物理）
    bad = [c.derivative_id for c in res.candidate_derivatives
           if c.derivative_id.startswith(("dHex", "Pen"))]
    eq(bad, [], "dHex/Pen 候选必须被门控剔除")
    ok(any("组成约束剔除" in n and "dHex" in n for n in res.notes),
       "必须记录 dHex 被可靠组成证据剔除")


@case("REV-ACC-2", "EI 糖种不写死：糖组含 Gal/Glc/Man，不凭排序输出单一糖名")
def _acceptance_sugar_ring():
    _ensure_refs()
    ions = {59: "+", 71: "+", 86: "+", 101: "+", 112: "+", 129: "red", 143: "+",
            161: "+", 172: "+", 188: "+", 202: "+", 218: "+", 232: "+", 245: "+"}
    res = analyze_component(
        ComponentInput(component_id="acc_46gal_sr", evidence_type=EvidenceType.qualitative_ion_list,
                       ions=ions, spectrum_completeness=SpectrumCompleteness.full_acquired_range,
                       provenance="observed"),
        AnalysisContext(analysis_id="acc2", sample_reduction_label="NaBD4",
                        composition_detected={"Gal"}, top_k=6),
    )
    sg = res.interpretation.residue_linkage.detail["sugar_group"]
    ok("Gal" in sg, "糖组应包含 Gal")
    ok(len(sg) > 1, "EI 不可分糖种应为糖组而非单一糖名（不写死）")


@case("REV-ACC-3", "证据分级（P6）+ 三段式（P7）：置信度=候选，第3段恒 gated 未确认")
def _acceptance_tier():
    _ensure_refs()
    ions = {59: "+", 71: "+", 86: "+", 101: "+", 112: "+", 129: "red", 143: "+",
            161: "+", 172: "+", 188: "+", 202: "+", 218: "+", 232: "+", 245: "+"}
    res = analyze_component(
        ComponentInput(component_id="acc_46gal_t", evidence_type=EvidenceType.qualitative_ion_list,
                       ions=ions, spectrum_completeness=SpectrumCompleteness.full_acquired_range,
                       provenance="observed"),
        AnalysisContext(analysis_id="acc3", sample_reduction_label="NaBD4",
                        composition_detected={"Gal"}, top_k=6),
    )
    eq(res.derivative_identity_level.value, DerivativeIdentityLevel.candidate.value,
       "定性离子表上限为候选")
    eq(res.interpretation.confidence_tier, "候选", "整体置信度必须为候选")
    eq(res.interpretation.sequence.gated, True, "第3段必须被证据门控")
    eq(res.interpretation.sequence.tier, "未确认", "第3段恒未确认")
    ok("NMR" in res.interpretation.sequence.text, "第3段必须提示需 NMR/MS-MS/酶解")


@case("REV-ACC-4", "selected_ions 不用缺失负证据，也不启用固定MW硬门控")
def _acceptance_mw_not_hard_for_selected():
    _ensure_refs()
    # 与 REV-ACC-1 相同的低质量诊断离子，但声明为 selected_ions（无完整高质量区）。
    # 这些离子会被 estimate_molecular_weight 误估出 M≈158，若执行硬门控则全灭。
    ions = {59: "+", 71: "+", 86: "+", 101: "+", 112: "+", 129: "red", 143: "+",
            161: "+", 172: "+", 188: "+", 202: "+", 218: "+", 232: "+", 245: "+"}
    res = analyze_component(
        ComponentInput(component_id="acc_46gal_sel", evidence_type=EvidenceType.qualitative_ion_list,
                       ions=ions, spectrum_completeness=SpectrumCompleteness.selected_ions,
                       provenance="observed"),
        AnalysisContext(analysis_id="acc4", sample_reduction_label="NaBD4",
                        composition_detected={"Gal"}, top_k=6),
    )
    ok(len(res.candidate_derivatives) > 0, "非全扫描定性输入仍应产生候选，不能被 MW 误杀")
    top = res.candidate_derivatives[0]
    eq(top.o_methyl_positions, [4, 6], "Top1 仍应为 4,6-二-O-甲基")
    ok(top.derivative_id.startswith("Hex"), "Top1 仍应为己糖骨架")
    ok(any("不启用分子量/骨架硬门控" in n for n in res.notes),
       "应明确记录不执行固定MW硬门控")
    ok(all(not d.score_components.get("missing_strong_penalty_applied")
           for d in res.candidate_derivatives),
       "selected_ions 不得把未列参考强峰当负证据")


@case("REV-ACC-5", "糖种按 EI 匹配质量优先排序（修改点 5）：4,6 案例 Gal/Man > Glc 不写死")
def _acceptance_sugar_ranking():
    _ensure_refs()
    # 方案验收谱：基峰 129、强峰 161，且 143/188 为红峰（4,6-Gal/Man 参考含、4,6-Glc 缺）。
    ions = {59: "+", 71: "+", 86: "red", 101: "red", 112: "+", 129: "red",
            143: "red", 161: "red", 172: "+", 188: "red", 202: "red",
            218: "+", 232: "+", 245: "red"}
    res = analyze_component(
        ComponentInput(component_id="acc_46gal_rank", evidence_type=EvidenceType.qualitative_ion_list,
                       ions=ions, spectrum_completeness=SpectrumCompleteness.full_acquired_range,
                       provenance="observed"),
        AnalysisContext(analysis_id="acc5", sample_reduction_label="NaBD4",
                        composition_detected={"Gal"}, top_k=6),
    )
    detail = res.interpretation.residue_linkage.detail
    ranked = detail["sugar_ranked"]
    ok("Gal" in ranked and "Glc" in ranked and "Man" in ranked,
       "糖组应含 Gal/Glc/Man 全部三种己糖")
    # 优先度：4,6-Gal/Man 参考含 143/188 → 匹配度高于缺 143/188 的 4,6-Glc → Glc 排末位
    ok(ranked[-1] == "Glc", f"4,6-Glc 因缺 143/188 应排末位，实际 {ranked}")
    ok(ranked[0] in ("Gal", "Man"), f"首位应为 Gal/Man（EI 匹配度最高），实际 {ranked[0]}")
    ok(" > " in res.interpretation.residue_linkage.text, "应出现优先度分隔 ' > '")
    ok("EI 不可分" in res.interpretation.residue_linkage.text, "Gal/Man 同组应标注 EI 不可分")


@case("REV-ACC-6", "完整实测峰表可对缺失参考强峰作软降权，但不得称为signature")
def _acceptance_signature_penalty():
    _ensure_refs()
    ions = {59: "+", 71: "+", 86: "red", 101: "red", 112: "+", 129: "red",
            143: "red", 161: "red", 172: "+", 188: "red", 202: "red",
            218: "+", 232: "+", 245: "red"}
    res = analyze_component(
        ComponentInput(component_id="acc_46gal_sig", evidence_type=EvidenceType.qualitative_ion_list,
                       ions=ions, spectrum_completeness=SpectrumCompleteness.full_acquired_range,
                       provenance="observed"),
        AnalysisContext(analysis_id="acc6", sample_reduction_label="NaBD4",
                        composition_detected={"Gal"}, top_k=12),
    )
    # 正确候选 Hex|4,6 的参考强峰召回应较高
    hex46 = next((c for c in res.candidate_derivatives
                  if c.derivative_id.startswith("Hex|Me4.6")), None)
    ok(hex46 is not None, "应有 Hex|Me4.6 候选")
    sig = hex46.score_components.get("reference_strong_recall")
    ok(sig is not None and sig >= 0.5, f"正确候选参考强峰召回应较高，实际 {sig}")
    ok(hex46.score_components.get("missing_strong_penalty_applied") is True,
       "完整实测峰表才允许启用缺失参考强峰软降权")
    # 即便不门控（屏蔽门控后）dHex 也应因缺 129/161 特征离子而显著低于 Hex|4,6
    dhex = [c for c in res.candidate_derivatives if c.derivative_id.startswith("dHex")]
    if dhex:
        ok(hex46.score > dhex[0].score,
           f"Hex|4,6 应高于 dHex（检索排序），{hex46.score} vs {dhex[0].score}")


@case("REV-ACC-7", "图片/文档推导数据即使误标 full，也不得启用缺失参考强峰负证据")
def _derived_never_uses_negative_evidence():
    _ensure_refs()
    ions = {59: "+", 71: "+", 87: "red", 100: "+", 116: "+", 129: "red",
            143: "+", 160: "+", 171: "+", 189: "red", 202: "+", 218: "+",
            231: "+", 245: "+", 262: "+"}
    res = analyze_component(
        ComponentInput(component_id="derived_guard", evidence_type=EvidenceType.qualitative_ion_list,
                       ions=ions, spectrum_completeness=SpectrumCompleteness.full_acquired_range,
                       provenance="derived_from_image"),
        AnalysisContext(analysis_id="derived_guard", sample_reduction_label="NaBD4", top_k=6),
    )
    ok(res.candidate_derivatives, "推导数据仍应产生候选")
    ok(all(not d.score_components.get("missing_strong_penalty_applied")
           for d in res.candidate_derivatives),
       "推导数据不得使用缺失负证据")
    ok(any("不得借‘完整扫描’" in n for n in res.notes), "应明确记录证据防护")
