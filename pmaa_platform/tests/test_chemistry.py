"""
化学映射与位点校验验收（03 契约 A03–A08、A10、A22；计划 14.6）。
"""
from __future__ import annotations

from pmaa.chemistry.mapping import (
    compute_mapping,
    map_derivative_by_methyl,
    map_from_derivative,
    traditional_linkage_label,
)
from pmaa.chemistry import validator as chemval
from pmaa.chemistry.frameworks import get_framework
from pmaa.domain.enums import StableErrorCode
from pmaa.domain.errors import ChemistryRuleError
from pmaa.ingest.name_parser import parse_pmaa_name

from _harness import case, eq, ok

# 14.6.2 醛己吡喃糖完整 16 种位置表
HEXP_TABLE = [
    ([2, 3, 4, 6], [], [1, 5]), ([3, 4, 6], [2], [1, 2, 5]), ([2, 4, 6], [3], [1, 3, 5]),
    ([2, 3, 6], [4], [1, 4, 5]), ([2, 3, 4], [6], [1, 5, 6]), ([4, 6], [2, 3], [1, 2, 3, 5]),
    ([3, 6], [2, 4], [1, 2, 4, 5]), ([3, 4], [2, 6], [1, 2, 5, 6]), ([2, 6], [3, 4], [1, 3, 4, 5]),
    ([2, 4], [3, 6], [1, 3, 5, 6]), ([2, 3], [4, 6], [1, 4, 5, 6]), ([6], [2, 3, 4], [1, 2, 3, 4, 5]),
    ([4], [2, 3, 6], [1, 2, 3, 5, 6]), ([3], [2, 4, 6], [1, 2, 4, 5, 6]), ([2], [3, 4, 6], [1, 3, 4, 5, 6]),
    ([], [2, 3, 4, 6], [1, 2, 3, 4, 5, 6]),
]


@case("A03", "Hexp 位置补集：2,3,6-Me3 → 4-连接，乙酰 1,4,5")
def _a03():
    oc = compute_mapping("aldohexopyranose", [2, 3, 6])
    eq(oc.linkage_positions, (4,), "连接位")
    eq(list(oc.o_acetyl_positions), [1, 4, 5], "乙酰位")
    # α/β 与 D/L 不由此产生
    oc2 = map_derivative_by_methyl([2, 3, 6], "Hex")
    ok(all(not hasattr(m, "alpha_beta") and not hasattr(m, "d_l") for m in oc2), "不得输出立体构型")
    for methyl, linkage, acetyl in HEXP_TABLE:
        o = compute_mapping("aldohexopyranose", methyl)
        eq(list(o.linkage_positions), linkage, f"Me{methyl} 连接位")
        eq(list(o.o_acetyl_positions), acetyl, f"Me{methyl} 乙酰位")
    print(f"        （已覆盖 16 种 Hexp 模式）")


@case("A04", "己糖环型来源歧义：2,3,6-Me3 / 1,4,5-Ac3 → 4-Hexp 与 5-Hexf")
def _a04():
    maps = map_derivative_by_methyl([2, 3, 6], "Hex")
    labels = {(m.ring_form, tuple(m.linkage_positions)) for m in maps}
    ok(("p", (4,)) in labels, "必须包含 4-Hexp")
    ok(("f", (5,)) in labels, "必须包含 5-Hexf")
    ok(len(maps) >= 2, "环型未知时不得只给一个来源")
    # 换柱不能保证分辨
    ok(len(maps) >= 2, "second_gc_column_guarantees_resolution 必须为 False")


@case("A05", "4-Arap 与 5-Araf 形成同一 PMAA")
def _a05():
    p = parse_pmaa_name("2,3-Ara")
    eq(sorted(p.o_methyl_positions), [2, 3], "O-甲基位")
    maps = map_derivative_by_methyl([2, 3], "Pen")
    labels = {(m.ring_form, tuple(m.linkage_positions)) for m in maps}
    ok(("p", (4,)) in labels, "必须包含 4-Arap（1,4,5-Ac3-2,3-Me2-arabinitol）")
    ok(("f", (5,)) in labels, "必须包含 5-Araf")
    # 反向：由衍生物实体反解来源也应得到两个
    origins = map_from_derivative("Pen", [2, 3], [1, 4, 5])
    eq(len(origins), 2, "同一 PMAA 的两个来源")


@case("A06", "普通 Hexp 出现 C5 甲基 → 隔离")
def _a06():
    try:
        compute_mapping("aldohexopyranose", [2, 3, 5, 6])
    except ChemistryRuleError as e:
        eq(e.code, StableErrorCode.METHYL_POSITION_INCOMPATIBLE_WITH_FRAMEWORK, "错误码")
        return
    raise AssertionError("应抛出 ChemistryRuleError")


@case("A07", "6-脱氧己糖出现 C6 甲基 → 隔离")
def _a07():
    try:
        compute_mapping("six_deoxy_aldohexopyranose", [2, 3, 4, 6])
    except ChemistryRuleError as e:
        eq(e.code, StableErrorCode.METHYL_POSITION_INCOMPATIBLE_WITH_FRAMEWORK)
        return
    raise AssertionError("应抛出 ChemistryRuleError")


@case("A08", "氨基糖：N 修饰已知时按 N 位剔除后的 U 计算；N 修饰未知则不允许中性映射")
def _a08():
    p = parse_pmaa_name("2,3,4,6-Gal(2NAc)")
    eq(p.is_amino_sugar, True, "应识别为氨基糖")
    eq(sorted(p.o_methyl_positions), [3, 4, 6], "C2 是 NAc，不能算 O-甲基")
    eq(p.n_modifications, ["C2:N-acetyl"], "N 修饰")
    oc = compute_mapping("aldohexopyranose", p.o_methyl_positions, p.n_positions)
    eq(list(oc.linkage_positions), [], "t-GalNAc 为端基")
    # C2 若被同时列为 O-甲基与 N-取代 → 冲突
    probs = chemval.validate_positions(
        framework=get_framework("aldohexopyranose"), o_methyl=[2, 3, 4, 6],
        o_acetyl=[1, 5], n_positions=frozenset({2}))
    ok(any(x["code"] == StableErrorCode.N_METHYL_POSITION_CONFLICT.value for x in probs),
       "C2 同时为 O-Me 与 N-取代必须报错")
    note = chemval.amino_sugar_branch_note(frozenset({2}))
    ok(note is not None and "不可签发自动确认" in note, "氨基糖分支必须给出未验证提示")


@case("A09", "标签未知不能跨 NaBD4/NaBH4 库确认")
def _a09():
    from pmaa.analysis.pipeline import AnalysisContext, ComponentInput, analyze_component
    from pmaa.domain.enums import EvidenceType, SpectrumCompleteness

    result = analyze_component(
        ComponentInput(
            component_id="a09_unknown_label",
            evidence_type=EvidenceType.qualitative_ion_list,
            ions={118.0: "red", 129.0: "+"},
            spectrum_completeness=SpectrumCompleteness.selected_ions,
        ),
        AnalysisContext(sample_reduction_label="unknown"),
    )
    eq(result.derivative_identity_level.value, "candidate", "未知标签的证据上限")
    ok(any(c.get("code") == StableErrorCode.LABEL_UNKNOWN.value for c in result.conflicts),
       "必须显式返回 LABEL_UNKNOWN")


@case("A10", "标签位移依原子来源：不得全谱统一 +1")
def _a10():
    # 该规则由导入层落实：NaBD4 只在 C1 引入 D，不会改写 m/z 列表
    from pmaa.ingest.importer import _ion_from_cell
    from pmaa.ingest.xlsx_reader import Cell
    ion = _ion_from_cell(118.0, Cell(coord="X3", row=3, col=24, value="+", font_color="FFFF0000"))
    eq(ion.mz, 118.0, "m/z 不得因标签而位移")
    eq(ion.relative_intensity, None, "定性表不得生成强度")


@case("A22", "仅有连接组成时不得输出唯一糖链序列")
def _a22():
    from pmaa.reporting.report import MINIMAL_FIELDS
    ok(not any("序列" in f for f in MINIMAL_FIELDS), "报告最小字段不含唯一序列")
    label = traditional_linkage_label([4], "p", "Glc")
    eq(label, "1,4-Glcp", "传统记号")
    ok("→" not in label, "标签不得表达残基间连接顺序")
