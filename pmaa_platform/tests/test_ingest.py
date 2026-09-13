"""
导入与峰表解析验收（A01、A02、A20、A23；14.8 导入流程）。
"""
from __future__ import annotations

import json

from pmaa.domain.enums import EvidenceType, IntensityClass
from pmaa.domain.errors import IngestError
from pmaa.domain.models import Ion, IonObservation, MethodSnapshot, ReductionLabel, ReferenceRecord, SourceInfo
from pmaa.ingest.importer import EISummaryTableImporter
from pmaa.ingest.name_parser import parse_pmaa_name
from pmaa.ingest.peak_table import parse_peak_table

from _harness import EI_TABLE, case, eq, ok

TABLE_AVAILABLE = __import__("pathlib").Path(EI_TABLE).exists()


def _minimal_record(ions, et=EvidenceType.qualitative_ion_list):
    return ReferenceRecord(
        record_id="r1", derivative_name_raw="2,3,6-Gal", parent_sugar="Gal", ring_form="p",
        o_methyl_positions=[2, 3, 6], o_acetyl_positions=[1, 4, 5], nitrogen_modifications=[],
        reduction_label=ReductionLabel(anomeric_reduction="NaBD4", carboxyl_reduction="none",
                                       methylation_reagent="CH3I", label_positions=[]),
        evidence_type=et,
        spectrum_completeness="selected_ions",
        ions=ions,
        source=SourceInfo(title="t", url=None, file_sha256=None, locator="Sheet!R1",
                          access_depth="local_raw", evidence_tier="E2",
                          redistribution_status="restricted"),
        method=MethodSnapshot(method_id=None, ei_energy_ev=70.0, scan_range_mz=[45, 350], retention=None),
        review_status="reference_only",
    )


@case("A01", "定性符号不是强度：+ 不得变成数值，禁止余弦匹配")
def _a01():
    # 模型层硬约束：定性记录携带强度即校验失败
    bad = Ion(mz=87, observation=IonObservation.present, relative_intensity=100,
              intensity_class=IntensityClass.reported_present, source_cell="B1")
    try:
        _minimal_record([bad])
    except Exception as e:
        ok("NUMERIC_INTENSITY_FORBIDDEN" in str(e), f"应报 NUMERIC_INTENSITY_FORBIDDEN，实际 {e}")
    else:
        raise AssertionError("定性记录携带强度居然通过了校验")

    # 峰表层：定性表里出现数值 → 明确报错
    try:
        parse_peak_table({"components": [{
            "component_id": "p1", "evidence_type": "qualitative_ion_list",
            "ions": [{"mz": 118, "marker": "100"}]}]})
    except IngestError as e:
        ok("强度" in str(e) or "INTENSITY" in str(e).upper(), f"应拒绝把数值当定性标记：{e}")
    else:
        raise AssertionError("定性表出现数值未被拒绝")

    # 颜色语义只记为 reported_gt20pct，不换算数值
    from pmaa.ingest.importer import _ion_from_cell
    from pmaa.ingest.xlsx_reader import Cell
    red = _ion_from_cell(118.0, Cell(coord="X3", row=3, col=1, value="+", font_color="FFFF0000"))
    eq(red.intensity_class, IntensityClass.reported_gt20pct, "红色语义")
    eq(red.relative_intensity, None, "红色不得换算为强度值")


@case("A02", "未列离子不是未检出：不得硬排除")
def _a02():
    from pmaa.ingest.importer import _ion_from_cell
    # 空单元格不生成任何离子行 —— 既不标 not_detected，也不参与排除
    eq(_ion_from_cell(233.0, None), None, "空单元格不应生成离子")
    from pmaa.search.qualitative import QualitativeIndex, RefEntry
    idx = QualitativeIndex([
        RefEntry("refA", {118.0: IntensityClass.reported_present, 129.0: IntensityClass.reported_present}),
        RefEntry("refB", {118.0: IntensityClass.reported_present}),
    ])
    # 查询只报 118：refB 不应被"未报 129"硬排除
    res = {s.record_id: s.score for s in idx.search({118.0: IntensityClass.reported_present})}
    ok(res["refB"] > 0, "未列出的离子不得导致候选被排除")


@case("A20", "分装不是独立制备")
def _a20():
    from pmaa.analysis.qc import evaluate_batch_qc
    r = evaluate_batch_qc(independent_preparations=1, post_methylation_aliquots=4)
    chk = [c for c in r.checks if c.code == "INDEPENDENT_PREPARATION"][0]
    eq(chk.evidence["independent_preparations"], 1, "独立制备次数")
    ok("不计入" in chk.message, "分装必须显式声明不计入独立制备")


@case("A23", "定性表未提供色谱时间序列 → 不评估共洗脱，定量不可用")
def _a23():
    from pmaa.analysis.pipeline import AnalysisContext, ComponentInput, analyze_component
    from pmaa.domain.enums import SpectrumCompleteness
    if not TABLE_AVAILABLE:
        print("        （跳过：未找到 EI 汇总表，使用空库路径）")
    comp = ComponentInput(component_id="p1", evidence_type=EvidenceType.qualitative_ion_list,
                          ions={118.0: "red", 129.0: "+"},
                          spectrum_completeness=SpectrumCompleteness.selected_ions)
    res = analyze_component(comp, AnalysisContext())
    # A23：定性表且无色谱时间序列 → 定量不可用
    eq(res.quantification_level.value, "unavailable", "无峰面积时定量等级应为 unavailable")
    ok("FULL_EI" in res.missing_evidence, "必须标记缺完整 EI")
    ok(res.derivative_identity_level.value != "confirmed", "定性表不得给出 confirmed")
    # 给了面积但仍无校准 → 只能是面积占比
    comp2 = ComponentInput(component_id="p1", evidence_type=EvidenceType.qualitative_ion_list,
                           ions={118.0: "red", 129.0: "+"}, area=1000.0,
                           spectrum_completeness=SpectrumCompleteness.selected_ions)
    res2 = analyze_component(comp2, AnalysisContext())
    eq(res2.quantification_level.value, "area_only", "有面积无校准应为 area_only")
    ok("CALIBRATION_MISSING" in res2.missing_evidence, "必须标记缺校准")


@case("A23b", "源表每一行都能追溯到具体单元格")
def _a23b():
    if not TABLE_AVAILABLE:
        print("        （跳过：EI 汇总表不可用）")
        return
    imp = EISummaryTableImporter(source_title="test")
    rep = imp.preview(EI_TABLE)
    ok(all(r.source_row_id.startswith("Sheet1!R") for r in rep.rows), "每条记录必须有源行定位")
    ok(rep.rows[0].preview["ion_count"] > 0, "应抽取到离子")
    print(f"        （{len(rep.rows)} 行：入库 {rep.imported}，氨基糖隔离 {rep.quarantined}，错误 {rep.isolated}）")


@case("A23c", "源表连接方式与规则推导零冲突（N 位修正后）")
def _a23c():
    if not TABLE_AVAILABLE:
        print("        （跳过：EI 汇总表不可用）")
        return
    imp = EISummaryTableImporter(source_title="test")
    rep = imp.preview(EI_TABLE)
    conflicts = [r for r in rep.rows if r.preview.get("linkage_conflict")]
    eq(len(conflicts), 0, "不应存在源表与规则的连接方式冲突")


@case("A23d", "名称解析：氨基糖的 N 位不得计入 O-甲基")
def _a23d():
    p = parse_pmaa_name("2,3,4-Gal(2NAc)")
    eq(sorted(p.o_methyl_positions), [3, 4], "O-甲基位应剔除 C2")
    eq(p.n_modifications, ["C2:N-acetyl"], "N 修饰")
    p2 = parse_pmaa_name("2,3,4,6-Glc")
    eq(sorted(p2.o_methyl_positions), [2, 3, 4, 6], "普通糖全部为 O-甲基")
    eq(p2.is_amino_sugar, False, "非氨基糖")
    p3 = parse_pmaa_name("2,3,4-Fuc")
    eq(p3.skeleton, "dHex", "Fuc 应判为 6-脱氧己糖")


@case("A23e", "名称解析：支持 CCRC 的 T-、p/f 与 NAc 后缀")
def _a23e():
    from pmaa.ingest.importer import _parse_source_linkage

    terminal = parse_pmaa_name("T-Galp")
    eq(terminal.parent_sugar, "Gal", "Galp 应规范为 Gal")
    eq(terminal.default_ring, "p", "p 后缀应保留吡喃型")
    eq(terminal.o_methyl_positions, [2, 3, 4, 6], "terminal Galp 应展开全部 O-甲基位")

    ribf = parse_pmaa_name("2,3,5-Ribf")
    eq(ribf.parent_sugar, "Rib", "Ribf 应规范为 Rib")
    eq(ribf.skeleton, "Pen", "Ribf 应识别为戊糖")
    eq(ribf.default_ring, "f", "f 后缀应保留呋喃型")

    amino = parse_pmaa_name("3-GalpNAc")
    eq(amino.parent_sugar, "Gal", "GalpNAc 应规范为 Gal")
    eq(amino.n_modifications, ["C2:N-acetyl"], "NAc 后缀应显式记录 C2 N-乙酰")
    eq(amino.o_methyl_positions, [3], "C2 不得误计为 O-甲基")

    terminal_amino = parse_pmaa_name("T-GlcpNAc")
    eq(terminal_amino.o_methyl_positions, [3, 4, 6],
       "terminal GlcpNAc 应展开 O 位并剔除 C2 N 位")
    eq(_parse_source_linkage("1,2,4,6-GalpNAc", frozenset({2})), [4, 6],
       "源表 NAc 位不得误判为糖苷连接位")


@case("A23f", "残基来源映射重导仅替换当前源行")
def _a23f():
    from pmaa.data import repositories as repo

    did = "Hex|OMe:2,3,6|OAc:1,4,5|label:test"
    repo.DerivativeRepo.upsert(
        did, skeleton="Hex", o_methyl=[2, 3, 6], o_acetyl=[1, 4, 5],
        label_key="test", display_name="test derivative",
    )
    for source_id, sugar in (("source-record-a", "Gal"), ("source-record-b", "Glc")):
        repo.ResidueMappingRepo.add(
            did, framework_id="Hex_p", parent_sugar=sugar, ring_form="p",
            linkage_positions=[4], precondition="test", source_record_id=source_id,
        )
    repo.ResidueMappingRepo.clear_for_source_record(did, "source-record-a")
    remaining = repo.ResidueMappingRepo.by_derivative(did)
    eq(len(remaining), 1, "只应保留另一源行贡献的映射")
    eq(remaining[0].source_record_id, "source-record-b", "不得跨来源误删")
