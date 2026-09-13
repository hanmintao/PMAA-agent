"""
EI 汇总表导入：原始行 → 规范化参考条目 → 化学复核 → 入库/隔离（M02 + M03）。

严格遵循 14.8 的九步导入流程，特别是：
  3. 红色只记语义，不换算成强度；
  5. 位点范围按骨架校验；
  6. 源表写出的"连接方式"必须重新做化学映射并比对，冲突进隔离队列；
  7. 多来源同名记录各自保留，不拼成"超级谱"；
  8. 只有离子集合的条目可进候选库，但不能充当生产确认/定量标准。
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..chemistry import validator as chemval
from ..chemistry.frameworks import skeleton_of
from ..chemistry.mapping import (
    canonical_derivative_key,
    compute_mapping,
    derivative_display_name,
    map_from_derivative,
    traditional_linkage_label,
)
from ..config import PARSER_VERSION
from ..data import repositories as repo
from ..domain.enums import (
    EvidenceTier,
    EvidenceType,
    IntensityClass,
    IonObservation,
    ReferenceReviewStatus,
    SpectrumCompleteness,
    StableErrorCode,
)
from ..domain.models import (
    Ion,
    LabelPosition,
    MethodSnapshot,
    ReductionLabel,
    ReferenceRecord,
    ResidueMapping,
    SourceInfo,
)
from .name_parser import parse_pmaa_name
from .xlsx_reader import SheetDump, SummaryTableLayout, detect_summary_layout, read_workbook

RULE_VERSION_USED = "rules_v1.0.0"


@dataclass
class ImportRowResult:
    source_row_id: str
    derivative_name_raw: str
    status: str                       # imported / quarantined / isolated
    record_id: str | None = None
    messages: list[str] = field(default_factory=list)
    error_codes: list[str] = field(default_factory=list)
    derived_linkage: list[int] = field(default_factory=list)
    source_linkage_raw: str | None = None
    preview: dict[str, Any] = field(default_factory=dict)


@dataclass
class ImportReport:
    source_file_id: str
    filename: str
    sha256: str
    layout: dict[str, Any]
    semantics: dict[str, Any]
    rows: list[ImportRowResult] = field(default_factory=list)

    @property
    def imported(self) -> int:
        return sum(1 for r in self.rows if r.status == "imported")

    @property
    def quarantined(self) -> int:
        return sum(1 for r in self.rows if r.status == "quarantined")

    @property
    def isolated(self) -> int:
        return sum(1 for r in self.rows if r.status == "isolated")

    def summary(self) -> dict[str, Any]:
        return {
            "source_file_id": self.source_file_id,
            "filename": self.filename,
            "sha256": self.sha256,
            "total_rows": len(self.rows),
            "imported": self.imported,
            "quarantined": self.quarantined,
            "isolated": self.isolated,
            "layout": self.layout,
            "detected_semantics": self.semantics,
        }


# ---------------------------------------------------------------------- 单元格 → 离子
def _ion_from_cell(mz: float, cell) -> Ion | None:
    """
    "+" → present；红色 "+" → reported_gt20pct；空 → **不生成记录**。

    空单元格在本表中是"未报告"，不是"未检出"（03 契约 A02）。
    我们不为未报告的 m/z 生成 not_detected 行，避免下游把它当排除证据。
    """
    if cell is None:
        return None
    val = str(cell.value).strip()
    if val in ("+", "＋", "√", "v", "V", "Y", "y"):
        cls = IntensityClass.reported_gt20pct if cell.is_red else IntensityClass.reported_present
        return Ion(mz=mz, observation=IonObservation.present, relative_intensity=None,
                   intensity_class=cls, source_cell=cell.coord)
    if val in ("-", "—", "nd", "ND"):
        # 源表显式写"无"：同样只记为 present=False 的未报告，
        # 因为定性表没有扫描覆盖与检测阈记录，不能升级为 not_detected
        return None
    return None


def _parse_source_linkage(raw: str | None) -> list[int] | None:
    """解析源表"连接方式"列，如 "1,3,4,6-Gal" → [3,4,6]（去掉异头碳 1 与糖名）。"""
    if not raw:
        return None
    head = raw.split("-")[0]
    nums = []
    for part in head.split(","):
        part = part.strip()
        if part.isdigit():
            nums.append(int(part))
    if not nums:
        return None
    return [n for n in nums if n != 1]


# ---------------------------------------------------------------------- 主流程
class EISummaryTableImporter:
    def __init__(self, *, source_title: str, source_url: str | None = None,
                 evidence_tier: EvidenceTier = EvidenceTier.E2,
                 redistribution_status: str = "restricted",
                 method_id: str | None = None, ei_energy_ev: float | None = 70.0,
                 scan_range_mz: list[float] | None = None,
                 reviewer: str = "system"):
        self.source_title = source_title
        self.source_url = source_url
        self.evidence_tier = evidence_tier
        self.redistribution_status = redistribution_status
        self.method_id = method_id
        self.ei_energy_ev = ei_energy_ev
        self.scan_range_mz = scan_range_mz
        self.reviewer = reviewer

    # -------------------------------------------------- 预览（不写库）
    def preview(self, path: str | Path) -> ImportReport:
        return self._run(path, commit=False)

    # -------------------------------------------------- 提交
    def commit(self, path: str | Path) -> ImportReport:
        return self._run(path, commit=True)

    def _run(self, path: str | Path, *, commit: bool) -> ImportReport:
        p = Path(path)
        sheets, sha, size = read_workbook(p)
        dump = sheets[next(iter(sheets))]
        layout = detect_summary_layout(dump)

        file_id = f"src_{sha[:16]}"
        report = ImportReport(
            source_file_id=file_id, filename=p.name, sha256=sha,
            layout={
                "sheet": dump.name,
                "header_row": layout.header_row,
                "ion_columns": len(layout.ion_columns),
                "mz_range": [min(layout.ion_columns.values()), max(layout.ion_columns.values())]
                if layout.ion_columns else None,
                "data_rows": len(layout.data_rows),
                "section_rows": layout.section_rows,
                "note_rows": layout.note_rows,
                "merged_ranges": dump.merged_ranges[:10],
                "hidden_rows": dump.hidden_rows,
                "hidden_cols": dump.hidden_cols,
            },
            semantics=layout.detected_semantics,
        )

        # 标签语义：以原表注释为准；没有注释就保持 unknown（不猜）
        red_means = layout.detected_semantics.get("red_means", "unknown")
        reductant = layout.detected_semantics.get("reduction_label", "unknown")
        if red_means != "reported_gt20pct":
            # 语义未确认 → 红色降级为 reported_present，绝不臆造强度语义
            red_means = "unknown"

        if commit:
            repo.SourceFileRepo.upsert(
                file_id=file_id, filename=p.name, sha256=sha, byte_size=size,
                parser_version=PARSER_VERSION, import_kind="ei_reference_table",
                meta={"layout": report.layout, "semantics": layout.detected_semantics},
            )

        current_section = None
        for r in layout.data_rows:
            if r in layout.section_rows:
                current_section = layout.section_rows[r]
            res = self._process_row(
                dump=dump, layout=layout, row=r, section=current_section,
                file_id=file_id, sha=sha, red_means=red_means, reductant=reductant,
                commit=commit,
            )
            report.rows.append(res)

        if commit:
            repo.audit("source_file", file_id, "import_commit",
                       report.summary(), self.reviewer)
        return report

    # -------------------------------------------------- 单行
    def _process_row(self, *, dump: SheetDump, layout: SummaryTableLayout, row: int,
                     section: str | None, file_id: str, sha: str,
                     red_means: str, reductant: str, commit: bool) -> ImportRowResult:
        name_cell = dump.cell(row, layout.name_col)
        raw_name = str(name_cell.value).strip() if name_cell else ""
        source_row_id = f"{dump.name}!R{row}"
        res = ImportRowResult(source_row_id=source_row_id, derivative_name_raw=raw_name, status="isolated")

        parsed = parse_pmaa_name(raw_name)
        res.messages.extend(parsed.problems)

        # ---- 离子抽取（保留单元格坐标）
        ions: list[Ion] = []
        for col, mz in sorted(layout.ion_columns.items()):
            ion = _ion_from_cell(mz, dump.cell(row, col))
            if ion is not None:
                if red_means != "reported_gt20pct" and ion.intensity_class == IntensityClass.reported_gt20pct:
                    ion = ion.model_copy(update={"intensity_class": IntensityClass.reported_present})
                ions.append(ion)
        if not ions:
            res.messages.append("该行未抽取到任何离子，无法作为检索参考")
            res.error_codes.append("EMPTY_ION_SET")
            res.status = "isolated"
            return res

        # ---- 骨架与环型
        skeleton = parsed.skeleton
        if section:
            if "脱氧" in section:
                skeleton = skeleton or "dHex"
            elif "五碳" in section:
                skeleton = skeleton or "Pen"
            elif "六碳" in section:
                skeleton = skeleton or "Hex"

        # 源表注释：五碳糖只统计呋喃糖 → 记录为默认环型，但映射仍保留吡喃候选
        ring_form = parsed.default_ring if parsed.default_ring != "unknown" else None

        # ---- 化学复核
        from ..chemistry.frameworks import get_framework
        n_pos = parsed.n_positions
        problems: list[dict] = []
        if skeleton is None:
            problems.append({"code": "SKELETON_UNKNOWN",
                             "message": "无法推断骨架，按未审条目入库，不可作为确认依据",
                             "level": "warning"})
            framework_id = None
            derived_linkage: list[int] = []
            o_acetyl: list[int] | None = None
        else:
            # 先用默认环型对应的骨架算一次；失败则退化为遍历所有骨架取首个可用
            cand_fws = [f for f in _frameworks_for(skeleton, ring_form)]
            framework_id = None
            for fw in cand_fws:
                p = chemval.validate_positions(
                    framework=fw, o_methyl=parsed.o_methyl_positions,
                    o_acetyl=None, n_positions=n_pos)
                if not p:
                    framework_id = fw.id
                    break
            if framework_id is None:
                # 位点与所有已知骨架都不相容 → 硬错误，隔离（A06/A07）
                problems.append({
                    "code": StableErrorCode.METHYL_POSITION_INCOMPATIBLE_WITH_FRAMEWORK.value,
                    "message": f"O-甲基位 {parsed.o_methyl_positions} 与骨架 {skeleton} 的所有已知环型均不相容",
                })
                derived_linkage, o_acetyl = [], None
            else:
                fw = get_framework(framework_id)
                oc = compute_mapping(fw.id, parsed.o_methyl_positions, n_pos)
                derived_linkage = list(oc.linkage_positions)
                o_acetyl = list(oc.o_acetyl_positions)
                problems.extend(chemval.validate_positions(
                    framework=fw, o_methyl=parsed.o_methyl_positions,
                    o_acetyl=o_acetyl, n_positions=n_pos))
                if oc.requires_artifact_review:
                    problems.append({"code": "FULLY_SUBSTITUTED_REVIEW",
                                     "message": "全取代模式，需人工复核欠甲基化/修饰/污染",
                                     "level": "warning"})

        problems.extend(chemval.validate_sugar_skeleton(parsed.parent_sugar, skeleton))

        # ---- 与源表"连接方式"列比对（14.8 第 6 步）
        raw_linkage = None
        if layout.linkage_col:
            lc = dump.cell(row, layout.linkage_col)
            raw_linkage = str(lc.value).strip() if lc else None
        src_linkage = _parse_source_linkage(raw_linkage)
        linkage_conflict = False
        if src_linkage is not None and derived_linkage and sorted(src_linkage) != sorted(derived_linkage):
            linkage_conflict = True
            problems.append({
                "code": StableErrorCode.SOURCE_LINKAGE_CONFLICT.value,
                "message": f"源表标注连接方式 {raw_linkage}（受体位 {src_linkage}）"
                           f"与化学规则推导 {derived_linkage} 不一致",
                "level": "warning",
            })

        # ---- 氨基糖分支
        branch = "neutral_aldose"
        if parsed.is_amino_sugar:
            branch = "aminosugar"
            note = chemval.amino_sugar_branch_note(n_pos)
            if note:
                problems.append({"code": StableErrorCode.AMINO_SUGAR_BRANCH_UNVALIDATED.value,
                                 "message": note, "level": "warning"})

        # ---- 状态判定：硬错误 → 隔离；仅有警告 → 入库但标记 reference_only
        hard = [p for p in problems if p.get("level") != "warning"]
        if hard:
            res.status = "isolated"
            res.error_codes = sorted({p["code"] for p in hard})
        else:
            res.status = "quarantined" if parsed.is_amino_sugar else "imported"

        res.derived_linkage = derived_linkage
        res.source_linkage_raw = raw_linkage
        res.messages.extend(p["message"] for p in problems)
        res.preview = {
            "parent_sugar": parsed.parent_sugar,
            "skeleton": skeleton,
            "framework_id": framework_id,
            "o_methyl_positions": parsed.o_methyl_positions,
            "o_acetyl_positions": o_acetyl,
            "n_modifications": parsed.n_modifications,
            "ring_form": ring_form,
            "ion_count": len(ions),
            "red_ion_count": sum(1 for i in ions
                                 if i.intensity_class == IntensityClass.reported_gt20pct),
            "derived_linkage": derived_linkage,
            "source_linkage": raw_linkage,
            "linkage_conflict": linkage_conflict,
            "branch": branch,
        }

        if not commit:
            return res

        # ---- 入库
        record_id = f"ref_{sha[:8]}_r{row}"
        reduction_label = ReductionLabel(
            anomeric_reduction=reductant if reductant in ("NaBD4", "NaBH4") else "unknown",
            carboxyl_reduction="none",
            methylation_reagent="CH3I",
            label_positions=[LabelPosition(atom_position="C1", isotope="D", count=1)]
            if reductant == "NaBD4" else [],
        )
        record = ReferenceRecord(
            record_id=record_id,
            derivative_name_raw=raw_name,
            parent_sugar=parsed.parent_sugar,
            ring_form=ring_form,
            o_methyl_positions=parsed.o_methyl_positions,
            o_acetyl_positions=o_acetyl,
            nitrogen_modifications=parsed.n_modifications,
            reduction_label=reduction_label,
            evidence_type=EvidenceType.qualitative_ion_list,
            spectrum_completeness=SpectrumCompleteness.selected_ions,
            ions=ions,
            source=SourceInfo(
                title=self.source_title, url=self.source_url, file_sha256=sha,
                locator=source_row_id, access_depth="local_raw",
                evidence_tier=self.evidence_tier,
                redistribution_status=self.redistribution_status,
            ),
            method=MethodSnapshot(
                method_id=self.method_id, ei_energy_ev=self.ei_energy_ev,
                scan_range_mz=self.scan_range_mz, retention=None,
            ),
            review_status=(ReferenceReviewStatus.quarantined if res.status == "quarantined"
                           else ReferenceReviewStatus.reference_only),
            skeleton=skeleton,
            source_row_id=source_row_id,
            source_linkage_raw=raw_linkage,
            derived_linkage=derived_linkage,
            linkage_conflict=linkage_conflict,
            branch=branch,
            quarantine_reason="; ".join(res.messages)[:500] or None,
        )
        repo.ReferenceRepo.save(record, source_file_id=file_id)

        # ---- 化学实体 + 全部相容原残基来源（14.6.4：多来源必须保留）
        if skeleton and o_acetyl:
            did = canonical_derivative_key(skeleton, parsed.o_methyl_positions, o_acetyl)
            repo.DerivativeRepo.upsert(
                did, skeleton=skeleton, o_methyl=parsed.o_methyl_positions,
                o_acetyl=o_acetyl, label_key=f"{reductant}_CH3I",
                display_name=derivative_display_name(skeleton, parsed.o_methyl_positions, o_acetyl),
            )
            origins = map_from_derivative(skeleton, parsed.o_methyl_positions, o_acetyl, n_pos)
            if origins:
                repo.ResidueMappingRepo.clear_for_derivative(did)
            for oc in origins:
                repo.ResidueMappingRepo.add(
                    did, framework_id=oc.framework.id, parent_sugar=parsed.parent_sugar,
                    ring_form=oc.framework.ring_form, linkage_positions=list(oc.linkage_positions),
                    precondition="充分甲基化；无未建模 O/N 修饰"
                                 + ("；需人工复核全取代" if oc.requires_artifact_review else ""),
                    source_record_id=record_id,
                )
        res.record_id = record_id
        return res


def _frameworks_for(skeleton: str, ring_form: str | None):
    from ..chemistry.frameworks import frameworks_for_skeleton
    fws = frameworks_for_skeleton(skeleton)
    if ring_form:
        preferred = [f for f in fws if f.ring_form == ring_form]
        return preferred + [f for f in fws if f.ring_form != ring_form]
    return fws
