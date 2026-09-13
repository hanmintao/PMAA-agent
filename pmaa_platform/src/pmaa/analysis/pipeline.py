"""
组分候选生成与五维状态判定（M04 + M05 + M07 + M09 的判定部分）。

这是平台的分析核心。所有"能不能确认"的判断都集中在这里，
保证同一 (输入, 库版本, 规则版本) 得到同一结果（可重放）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..chemistry.mapping import (
    canonical_derivative_key,
    derivative_display_name,
    traditional_linkage_label,
)
from ..config import QUALITATIVE_EVIDENCE_CEILING, RULE_VERSION, SEARCH_DEFAULTS
from ..data import repositories as repo
from ..domain.enums import (
    DerivativeIdentityLevel,
    EvidenceType,
    IntensityClass,
    IonObservation,
    QCStatus,
    QuantificationLevel,
    ReferenceReviewStatus,
    ResidueMappingLevel,
    ReviewStatus,
    SpectrumCompleteness,
    StableErrorCode,
)
from ..domain.models import (
    Ambiguity,
    CandidateDerivative,
    CandidateResidue,
    ComponentResult,
    Interpretation,
    InterpretationSegment,
    Quantification,
)
from ..search.engine import load_library, load_numeric_library
from ..search.numeric import match_numeric
from ..search.qualitative import group_near_ties
from ..search.retention import check_retention
from ..chemistry.retention_index import load_ri_standards, match_species_by_ri
from .quantification import AREA_DENOMINATOR, quantification_for

_LEVEL_ORDER = ["unknown", "candidate", "probable", "confirmed"]


def _cap(a: str, b: str) -> str:
    """取两个等级中较低的那个作为证据上限。"""
    return _LEVEL_ORDER[min(_LEVEL_ORDER.index(a), _LEVEL_ORDER.index(b))]


@dataclass
class ComponentInput:
    component_id: str
    evidence_type: EvidenceType
    ions: dict[float, Any] = field(default_factory=dict)   # 定性：mz->"+"|"red"；数值：mz->强度
    peak_label: str | None = None
    rt: float | None = None
    area: float | None = None
    area_is: float | None = None
    spectrum_completeness: SpectrumCompleteness = SpectrumCompleteness.unknown
    detector: str = "EI_TIC"
    integration_method: str = "tic"
    unresolved_group_id: str | None = None
    # 数据出处：observed = 仪器直接导出；derived_* = 由图片/文档反解。
    # 后者不是实测值，证据上限会被压低，并要求人工校准确认。
    provenance: str = "observed"
    extraction_params: dict | None = None
    # 保留指数（RI）定种通道输入：observed RI（推荐）或 observed RT；
    # 二者皆空 → RI 通道静默（no-op），不改变现有结论。
    retention_index: float | None = None
    retention_time: float | None = None


@dataclass
class AnalysisContext:
    analysis_id: str = "unbound_analysis"
    method_id: str | None = None
    library_version: str = "library_dev"
    sample_reduction_label: str = "unknown"      # NaBD4 / NaBH4 / unknown
    composition_detected: set[str] = field(default_factory=set)
    composition_detection_limit_known: bool = False
    calibration: Any | None = None
    batch: str | None = None
    bridge_validated: bool = False
    qc_status: QCStatus = QCStatus.not_assessed
    top_k: int = 5
    # RI 定种容差覆盖（缺省回退 SEARCH_DEFAULTS）；透传给 match_species_by_ri。
    ri_tolerance: float | None = None
    ri_wide_tolerance: float | None = None


def _qualitative_query(ions: dict[float, Any]) -> dict[float, IntensityClass]:
    out: dict[float, IntensityClass] = {}
    for mz, marker in ions.items():
        m = str(marker).strip().lower()
        if m in ("red", "+red", "强", "r"):
            out[float(mz)] = IntensityClass.reported_gt20pct
        else:
            out[float(mz)] = IntensityClass.reported_present
    return out


def _normalise_sugar(value: str) -> str:
    """把常见长名称收敛到参考库使用的短名称，供组成约束比较。"""
    raw = str(value).strip()
    key = raw.lower().replace("-", "").replace("_", "").replace(" ", "")
    aliases = {
        "glucose": "Glc", "dglucose": "Glc", "glc": "Glc",
        "galactose": "Gal", "dgalactose": "Gal", "gal": "Gal",
        "mannose": "Man", "dmannose": "Man", "man": "Man",
        "fucose": "Fuc", "lfucose": "Fuc", "fuc": "Fuc",
        "rhamnose": "Rha", "lrhamnose": "Rha", "rha": "Rha",
        "arabinose": "Ara", "larabinose": "Ara", "ara": "Ara",
        "xylose": "Xyl", "dxylose": "Xyl", "xyl": "Xyl",
        "glcn": "GlcN", "glcnac": "GlcNAc", "galn": "GalN", "galnac": "GalNAc",
    }
    return aliases.get(key, raw)


def analyze_component(comp: ComponentInput, ctx: AnalysisContext) -> ComponentResult:
    index, by_id = load_library()
    missing: list[str] = []
    conflicts: list[dict] = []
    notes: list[str] = []

    # ------------------------------------------------------------ 证据上限
    ceiling = DerivativeIdentityLevel.confirmed.value
    if comp.evidence_type == EvidenceType.qualitative_ion_list:
        ceiling = _cap(ceiling, QUALITATIVE_EVIDENCE_CEILING)
        missing.append("FULL_EI")
        notes.append("输入为定性离子表：不生成数值强度，不做全谱余弦匹配（A01）")
    elif comp.spectrum_completeness != SpectrumCompleteness.full_acquired_range:
        ceiling = _cap(ceiling, "probable")
        missing.append("FULL_EI")

    if ctx.sample_reduction_label not in ("NaBD4", "NaBH4"):
        ceiling = _cap(ceiling, "candidate")
        missing.append("LABEL_UNKNOWN")
        conflicts.append({"code": StableErrorCode.LABEL_UNKNOWN.value,
                          "message": "样品还原试剂标签未知，不得跨标签库确认（A09）"})

    # 由图片/文档反解的数据不是实测值：证据上限压到 candidate，并记缺失项
    if comp.provenance.startswith("derived"):
        ceiling = _cap(ceiling, "candidate")
        missing.append("IMAGE_DERIVED")
        notes.append(
            "本组分数据由图片/文档反解得到（非仪器直接导出）：m/z 与强度均为推导值，"
            "仅用于候选检索，不得作为确认或定量依据；正式结论需以原始数据或仪器导出峰表为准。")

    # ------------------------------------------------------------ 检索
    # 先按记录检索更多条目，再按化学实体去重，最终取前 N 个**不同 PMAA 实体**
    # （计划 13.3：初始上下文预算按不同实体计，不是按记录行计）
    match_scores: dict[str, float] = {}
    sb_breakdowns: dict[str, object] = {}   # record_id -> ScoreBreakdown（P1/P3：透出 coverage/红峰/低覆盖）
    if comp.evidence_type == EvidenceType.qualitative_ion_list:
        q = _qualitative_query(comp.ions)
        # “未见参考强峰”只有在仪器直接导出的完整峰表中才可作软性负证据。
        # 图片/文档反解即使画面写着 Full Spectrum，也可能裁切、漏峰或轴标定偏移。
        allow_missing_strong_penalty = (
            comp.provenance == "observed"
            and comp.spectrum_completeness == SpectrumCompleteness.full_acquired_range
        )
        hits = index.search(
            q, top_k=max(ctx.top_k * 4, 20),
            allow_missing_reference_penalty=allow_missing_strong_penalty,
        )
        for h in hits:
            match_scores[h.record_id] = h.score
            sb_breakdowns[h.record_id] = h
    else:
        numeric_lib = load_numeric_library()
        if not numeric_lib:
            notes.append("参考库中没有数值谱记录，无法进行数值匹配，退回定性模式")
            q = _qualitative_query({mz: "+" for mz in comp.ions})
            hits = index.search(q, top_k=max(ctx.top_k * 4, 20))
            for h in hits:
                match_scores[h.record_id] = h.score
        else:
            nm = match_numeric({float(k): float(v) for k, v in comp.ions.items()},
                               numeric_lib, top_k=max(ctx.top_k * 4, 20))
            for m in nm:
                match_scores[m.record_id] = m.score

    # ------------------------------------------------------------ 候选衍生物
    candidates: list[CandidateDerivative] = []
    residue_candidates: list[CandidateResidue] = []
    ambiguities: list[Ambiguity] = []
    positive_evidence: list[dict] = []

    # -- 阶段一：按化学实体聚合（Gal/Glc/Man 的同一 PMAA 是同一实体，EI 不区分糖种）
    entities: dict[str, dict] = {}
    for rid, score in match_scores.items():
        entry = by_id[rid]
        meta = entry.meta
        skeleton = meta.get("skeleton")
        o_methyl = meta.get("o_methyl_positions") or []
        o_acetyl = meta.get("o_acetyl_positions") or []
        if not skeleton or not o_acetyl:
            continue
        did = canonical_derivative_key(skeleton, o_methyl, o_acetyl)
        e = entities.setdefault(did, {"score": score, "supported_by": [], "sugars": set(),
                                      "skeleton": skeleton, "o_methyl": o_methyl,
                                      "o_acetyl": o_acetyl, "conflicts": [], "levels": [],
                                      "sugar_scores": {}})
        e["score"] = max(e["score"], score)
        e["supported_by"].append(rid)
        # P1/P3：记录该 did 下得分最高的 ScoreBreakdown，用于透出 coverage/红峰/低覆盖
        sb = sb_breakdowns.get(rid)
        if sb is not None and (e.get("best_sb") is None
                               or getattr(sb, "score", 0) > getattr(e.get("best_sb"), "score", 0)):
            e["best_sb"] = sb
        if meta.get("parent_sugar"):
            e["sugars"].add(meta["parent_sugar"])
            # 修改点 5：记录每个糖种记录各自的 EI 匹配质量，供糖种优先排序
            # （如 4,6-Gal/Man 参考含 143/188，而 4,6-Glc 缺 → Glc 匹配度更低 → 排后）。
            if sb is not None and sb.score is not None:
                sg0 = meta["parent_sugar"]
                e["sugar_scores"][sg0] = max(e["sugar_scores"].get(sg0, 0.0), sb.score)

        # 同方法保留证据
        obs = [{"method_id": o.method_id, "kind": o.kind, "value": o.value,
                "unit": o.unit, "sd": o.sd}
               for o in repo.RetentionRepo.by_derivative(did, ctx.method_id)]
        if not obs and ctx.method_id:
            obs = [{"method_id": o.method_id, "kind": o.kind, "value": o.value,
                    "unit": o.unit, "sd": o.sd}
                   for o in repo.RetentionRepo.by_derivative(did)]
        rt_check = check_retention(comp.rt, ctx.method_id, obs,
                                   bridge_validated=ctx.bridge_validated)
        if rt_check.status == "no_observation":
            missing.append("RETENTION_EVIDENCE")
        elif rt_check.status == "incompatible":
            e["conflicts"].append({"code": StableErrorCode.RETENTION_METHOD_MISMATCH.value,
                                   "message": rt_check.message})

        # 参考条目等级约束：非本地验证条目不能支持 confirmed
        lvl = ceiling
        if meta.get("review_status") != ReferenceReviewStatus.locally_validated.value:
            lvl = _cap(lvl, "probable")
            missing.append("STANDARD_SAME_METHOD")
        if meta.get("review_status") == ReferenceReviewStatus.quarantined.value:
            lvl = _cap(lvl, "candidate")
            e["conflicts"].append(
                {"code": StableErrorCode.AMINO_SUGAR_BRANCH_UNVALIDATED.value,
                 "message": f"参考条目 {rid} 处于隔离状态（氨基糖/专项分支未验证），仅可作候选"})
        if meta.get("linkage_conflict"):
            e["conflicts"].append({"code": StableErrorCode.SOURCE_LINKAGE_CONFLICT.value,
                                   "message": f"参考条目 {rid} 的源表连接方式与规则推导不一致"})
        e["levels"].append(lvl)

    # ------------------------------------------------------------ 证据约束门控
    # 旧实现从 EI 碎片三联峰猜“分子离子”，再按骨架固定 MW 硬删候选。PMAA 的甲基/乙酰
    # 取代数会改变衍生物分子量，EI 分子离子还可能很弱或缺失，因此该启发式不能作为硬门。
    # 完整扫描状态只允许启用“缺参考强峰”的软降权，绝不自动推断分子量或糖骨架。
    if comp.spectrum_completeness == SpectrumCompleteness.full_acquired_range:
        if comp.provenance == "observed":
            notes.append(
                "已声明为仪器直接导出的完整峰表：未见参考强峰仅作软降权；"
                "不从 EI 碎片自动猜分子离子或糖骨架。")
        else:
            notes.append(
                "推导型图片/文档输入不得借‘完整扫描’启用负证据或分子量硬门控；"
                "仍按 selected-ion 候选检索解释。")
    else:
        notes.append(
            "峰表未证明完整：未列离子不作负证据，不启用分子量/骨架硬门控（A02）。")

    composition_allowed = {_normalise_sugar(x) for x in ctx.composition_detected}
    gated_entities: dict[str, dict] = {}
    for did, e in entities.items():
        # 单糖组成只有在“检测范围/检出能力已知且本次列表可视为完整”时才是硬约束。
        # 否则仍保留库外糖候选并在后面给软冲突，避免水解/衍生回收偏差造成误删（A17）。
        entity_sugars = {_normalise_sugar(x) for x in e["sugars"]}
        if (ctx.composition_detection_limit_known and composition_allowed and entity_sugars
                and entity_sugars.isdisjoint(composition_allowed)):
            notes.append(
                f"组成约束剔除：{did} 的来源糖 {sorted(entity_sugars)} 不在已验证完整的"
                f"单糖组成 {sorted(composition_allowed)} 中。")
            continue
        gated_entities[did] = e

    # 只保留分数最高的前 top_k 个不同实体（已通过可靠的组成证据约束）
    ordered = sorted(gated_entities.items(), key=lambda kv: -kv[1]["score"])[: ctx.top_k]
    low_library_match = bool(
        ordered
        and comp.evidence_type == EvidenceType.qualitative_ion_list
        and ordered[0][1]["score"] < SEARCH_DEFAULTS["qualitative_low_match_warning"]
    )
    if ordered:
        best = ordered[0][1]["score"]
        tied = [d for d, e in ordered if best - e["score"] <= SEARCH_DEFAULTS["near_tie_delta"]]
    else:
        tied = []

    for did, e in ordered:
        # 并列组处理：Top1 与并列者同等级（A13）
        lvl = min(e["levels"], key=_LEVEL_ORDER.index)
        if low_library_match:
            lvl = _cap(lvl, "unknown")
        if did in tied and len(tied) > 1:
            lvl = _cap(lvl, "candidate")
            notes.append(f"存在并列候选组 {tied}：不得只保留 Top1（A13）")
        conflicts.extend(e["conflicts"])

        # 糖种：同一 PMAA 若由多个糖种记录共同支持，则 EI 无法区分，标签不写死糖名
        sugar = None
        if len(e["sugars"]) == 1:
            sugar = next(iter(e["sugars"]))
        elif len(e["sugars"]) > 1:
            notes.append(f"{did} 由多个糖种记录共同支持（{sorted(e['sugars'])}）："
                         f"常规 EI 不能区分糖种，标签不写死糖名（14.11）")

        # 透出覆盖率 / 参考强峰召回 / 参考条目稀疏度（仅定性模式）
        sb = e.get("best_sb")
        cov = getattr(sb, "coverage", None) if sb is not None else None
        prec = getattr(sb, "precision", None) if sb is not None else None
        red_m = getattr(sb, "red_matched", None) if sb is not None else None
        red_t = getattr(sb, "red_total", None) if sb is not None else None
        low_cov = bool(getattr(sb, "low_coverage", False)) if sb is not None else False
        sig_rec = getattr(sb, "signature_recall", None) if sb is not None else None
        sig_m = getattr(sb, "signature_matched", None) if sb is not None else None
        sig_t = getattr(sb, "signature_total", None) if sb is not None else None
        missing_strong_penalty_applied = bool(
            getattr(sb, "missing_strong_penalty_applied", False)) if sb is not None else False

        candidates.append(CandidateDerivative(
            candidate_id=f"cand_{did}",
            derivative_id=did,
            derivative_name=derivative_display_name(e["skeleton"], e["o_methyl"], e["o_acetyl"]),
            o_methyl_positions=e["o_methyl"],
            o_acetyl_positions=e["o_acetyl"],
            score=round(e["score"], 6),
            score_components={"match_score": round(e["score"], 6),
                              "supporting_records": len(e["supported_by"]),
                              "distinct_parent_sugars": sorted(e["sugars"]),
                              "low_coverage_warning": low_cov,
                              "signature_recall": sig_rec,
                              "signature_matched": sig_m, "signature_total": sig_t,
                              "reference_strong_recall": sig_rec,
                              "reference_strong_matched": sig_m,
                              "reference_strong_total": sig_t,
                              "reference_ion_count": getattr(sb, "ref_ion_count", None) if sb is not None else None,
                              "missing_strong_penalty_applied": missing_strong_penalty_applied},
            identity_level=DerivativeIdentityLevel(lvl),
            supported_by=e["supported_by"],
            coverage=cov, precision=prec,
            red_matched=red_m, red_total=red_t,
            low_coverage_warning=low_cov,
        ))

        # 糖种：同一 PMAA 若由多个糖种记录共同支持，则 EI 无法区分，
        # 标签退回到骨架级别（如 1,6-Hexp），不写死糖名（14.11）
        sugar = None
        if len(e["sugars"]) == 1:
            sugar = next(iter(e["sugars"]))
        elif len(e["sugars"]) > 1:
            notes.append(f"{did} 由多个糖种记录共同支持（{sorted(e['sugars'])}）："
                         f"常规 EI 不能区分糖种，标签退回到骨架级别（14.11）")
        sugar_label = sugar or e["skeleton"]

        # ------------------------------------------------ 原残基来源（多来源必须全保留）
        mappings = repo.ResidueMappingRepo.by_derivative(did)
        labels: list[str] = []
        for m in mappings:
            label = traditional_linkage_label(m.linkage_positions, m.ring_form, sugar_label)
            if label in labels:
                continue
            labels.append(label)
            residue_candidates.append(CandidateResidue(
                derivative_id=did, residue_label=label,
                framework_id=m.framework_id, ring_form=m.ring_form,
                linkage_positions=list(m.linkage_positions),
                mapping_level=ResidueMappingLevel.ambiguous, parent_sugar=sugar,
            ))
            positive_evidence.append({
                "kind": "residue_mapping",
                "derivative_id": did,
                "residue_label": label,
                "source_record_id": m.source_record_id,
                "precondition": m.precondition,
            })

        if len(labels) > 1:
            # 同一 PMAA 的多个原始来源 → 必须保留的不可辨识关系（14.6.4 / A05 / A24）
            amb_id = f"AMB_{did}"
            ambiguities.append(Ambiguity(
                ambiguity_id=amb_id,
                # 必须与证据包中的 candidate_id 完全一致，否则后校验无法识别
                # “同一 PMAA 的多个来源”并强制把它们保留在同一排序组。
                candidate_ids=[f"cand_{did}::{label}" for label in labels],
                type="identical_derivative_origins",
                required_evidence_code="RING_FORM_EVIDENCE",
            ))
            missing.append("RING_FORM_EVIDENCE")
            notes.append(f"{did} 可由多个原残基来源形成；换柱或更高分数都不能恢复该信息（14.6.4）")

    # ------------------------------------------------------------ 等级汇总
    if not candidates:
        identity = DerivativeIdentityLevel.unknown
        mapping = ResidueMappingLevel.unsupported
    else:
        identity = DerivativeIdentityLevel(
            min((c.identity_level.value for c in candidates), key=_LEVEL_ORDER.index))
        distinct_labels = {r.residue_label for r in residue_candidates}
        if not residue_candidates:
            mapping = ResidueMappingLevel.unsupported
        elif len(distinct_labels) > 1:
            mapping = ResidueMappingLevel.ambiguous
        else:
            mapping = ResidueMappingLevel.resolved
        for r in residue_candidates:
            r.mapping_level = mapping

    if low_library_match:
        best_score = ordered[0][1]["score"]
        identity = DerivativeIdentityLevel.unknown
        missing.append("LIBRARY_COVERAGE_LOW")
        conflicts.append({
            "code": StableErrorCode.LOW_LIBRARY_MATCH.value,
            "message": (
                f"最高定性匹配分数仅 {best_score:.3f}，低于未验证的保守警戒线 "
                f"{SEARCH_DEFAULTS['qualitative_low_match_warning']:.2f}；可能是参考库未覆盖、"
                "m/z 轴校准错误或图片伪峰。保留候选供复核，但身份记为 unknown。"
            ),
        })

    # 参考条目本身收录离子较少时，提示排名对输入与库完整度敏感。
    low_cov_dids = [did for did, e in ordered
                    if getattr(e.get("best_sb"), "low_coverage", False)]
    if low_cov_dids:
        thr = SEARCH_DEFAULTS["low_coverage_ref_ion_threshold"]
        notes.append(
            f"存在参考条目稀疏（<{thr} 个收录离子，如 {low_cov_dids[0]}），"
            "其排名对上传完整度较敏感；建议补全参考库离子或补 RI/NMR 复核（评分公平性修正 P3）。")

    # ------------------------------------------------------------ 组成软约束（A17）
    if ctx.composition_detected:
        ei_sugars = {r.parent_sugar for r in residue_candidates if r.parent_sugar}
        extra = ei_sugars - ctx.composition_detected
        if extra and not ctx.composition_detection_limit_known:
            conflicts.append({"code": StableErrorCode.COMPOSITION_CONFLICT.value,
                              "message": f"PMAA 提示 {sorted(extra)}，组成未检出且检出能力未知："
                                         f"保留候选，只作软约束提示"})

    # ------------------------------------------------------------ 定量
    quant = quantification_for(
        comp.component_id, comp.area, calibration=ctx.calibration,
        detector=comp.detector, integration_method=comp.integration_method,
        batch=ctx.batch, area_is=comp.area_is,
        unresolved_group_id=comp.unresolved_group_id)
    if quant.mode != QuantificationLevel.calibrated_molar:
        missing.append("CALIBRATION_MISSING")
    if comp.unresolved_group_id:
        conflicts.append({"code": "COELUTION_UNRESOLVED",
                          "message": "共洗脱不可辨识：保持组合峰，不重复计量共享面积（A14）"})

    # ------------------------------------------------------------ 下一步证据建议
    next_steps: list[str] = []
    if "FULL_EI" in missing:
        next_steps.append("OBTAIN_FULL_EI")
    if "RING_FORM_EVIDENCE" in missing:
        next_steps.append("REVIEW_NMR_RING_EVIDENCE")
    if "RETENTION_EVIDENCE" in missing:
        next_steps.append("RUN_SAME_METHOD_STANDARD")
    if "STANDARD_SAME_METHOD" in missing:
        next_steps.append("RUN_SAME_METHOD_STANDARD")
    if "LABEL_UNKNOWN" in missing:
        next_steps.append("VERIFY_REDUCTION_LABEL")
    if "CALIBRATION_MISSING" in missing:
        next_steps.append("REVIEW_CALIBRATION")
    if "LIBRARY_COVERAGE_LOW" in missing:
        next_steps.extend(["VERIFY_MZ_AXIS", "EXPAND_REFERENCE_LIBRARY"])
    if len(candidates) > 1:
        next_steps.append("RUN_SECOND_COLUMN")
    if not next_steps:
        next_steps.append("MANUAL_REVIEW")

    # ------------------------------------------------------------ 三段式判读（修改方案 P5/P6/P7）
    # 明确分层：PMAA 身份 → 残基连接模式 → 多糖序列/重复单元；
    # 第3段必须有 NMR/MS-MS/酶解证据，否则恒为 gated 未确认，杜绝无证据的“完整结构”。
    _TIER = {"unknown": "未知", "candidate": "候选", "probable": "支持", "confirmed": "确认"}
    sugar_by_did = {did: sorted(e["sugars"]) for did, e in gated_entities.items()}
    top_did = candidates[0].derivative_id if candidates else None
    top_residues = [r for r in residue_candidates if r.derivative_id == top_did] if top_did else []
    ring_evidenced = "RING_FORM_EVIDENCE" not in set(missing)

    # ------------------------------------------------------------ RI 定种（保留指数通道，正交维度）
    # EI 定骨架+连接，RI 定糖种。无 observed RI/RT、无 method_id 或表中无标准 → 完全静默（no-op）。
    sugar_species: str | None = None
    species_evidence: dict[str, Any] | None = None
    ri_obs = comp.retention_index
    ri_use_time = False
    if comp.retention_time is not None:
        ri_obs = comp.retention_time
        ri_use_time = True
    if top_did and ri_obs is not None and ctx.method_id:
        top_e = gated_entities.get(top_did, {})
        top_skeleton = top_e.get("skeleton")
        top_methyl = tuple(top_e.get("o_methyl", []))
        if top_skeleton:
            ri_table = load_ri_standards()
            if ri_table:
                ri_result = match_species_by_ri(
                    ri_obs, method_id=ctx.method_id, skeleton=top_skeleton,
                    o_methyl_positions=top_methyl, table=ri_table,
                    tolerance=ctx.ri_tolerance, wide_tolerance=ctx.ri_wide_tolerance,
                    use_time=ri_use_time)
                if ri_result.status == "matched":
                    sugar_species = ri_result.sugar
                    species_evidence = {"channel": "RI", "method_id": ctx.method_id,
                                        "status": "matched", "delta": ri_result.delta}
                    notes.append(f"保留指数定种（{ctx.method_id}）：{ri_result.note}")
                elif ri_result.status == "ambiguous":
                    notes.append("保留指数定种存在歧义：" + ri_result.note)
                    missing.append("RETENTION_EVIDENCE")
                    species_evidence = {"channel": "RI", "method_id": ctx.method_id,
                                        "status": "ambiguous", "candidates": ri_result.candidates}
                elif ri_result.status == "nearest":
                    notes.append("保留指数定种为最近邻（超出精确容差）：" + ri_result.note)
                    missing.append("RETENTION_EVIDENCE")
                    species_evidence = {"channel": "RI", "method_id": ctx.method_id,
                                        "status": "nearest", "sugar": ri_result.sugar,
                                        "delta": ri_result.delta}
                elif ri_result.status == "no_hit":
                    notes.append("保留指数无匹配：" + ri_result.note)
                    missing.append("RETENTION_EVIDENCE")
                    species_evidence = {"channel": "RI", "method_id": ctx.method_id,
                                        "status": "no_hit", "nearest": ri_result.sugar,
                                        "delta": ri_result.delta}
                # no_data → 静默，不追加任何信息

    # 第1段：PMAA 身份（候选/支持）
    if top_did and candidates:
        top = candidates[0]
        pmaa_text = f"{top.derivative_name}（{ctx.sample_reduction_label}）"
        pmaa_detail = {"derivative_id": top.derivative_id,
                       "o_methyl_positions": top.o_methyl_positions,
                       "o_acetyl_positions": top.o_acetyl_positions}
    else:
        pmaa_text = "无匹配候选"
        pmaa_detail = {}
    seg1 = InterpretationSegment(tier=_TIER[identity.value], level=identity.value,
                                text=pmaa_text, gated=False, detail=pmaa_detail)

    # 第2段：残基连接模式（EI 不可分糖种/环型不写死，修改方案 P5）
    link_sets = sorted({tuple(r.linkage_positions) for r in top_residues})
    link_str = "、".join("/".join(str(x) for x in lp) for lp in link_sets) if link_sets else "未知"
    sg = sugar_by_did.get(top_did, [])
    sugar_ranked: list[str] = list(sg)
    if sugar_species:
        sg = [sugar_species]
        sugar_ranked = [sugar_species]
        sugar_txt = f"{sugar_species}（RI 定种）"
    else:
        sscores = gated_entities.get(top_did, {}).get("sugar_scores", {})
        delta = SEARCH_DEFAULTS["near_tie_delta"]
        if sscores and len(sg) > 1:
            # 修改点 5：糖种按各自 EI 匹配质量降序；相近者（≤ near_tie_delta）归并为一组，
            # 组间用 " > " 表示优先度（如 4,6 案例 Gal/Man 因参考含 143/188 排前、Glc 缺排后）。
            # 同一组内糖种 EI 不可分，不写死单一糖名。
            ordered = sorted(sg, key=lambda s: -sscores.get(s, 0.0))
            sugar_ranked = ordered
            groups: list[list[str]] = [[ordered[0]]]
            for s in ordered[1:]:
                if abs(sscores.get(s, 0.0) - sscores.get(groups[-1][0], 0.0)) <= delta:
                    groups[-1].append(s)
                else:
                    groups.append([s])
            sugar_txt = " > ".join("/".join(g) for g in groups)
            if any(len(g) > 1 for g in groups):
                sugar_txt += "（EI 不可分，按 O-甲基模式排序）"
            else:
                sugar_txt += "（按 EI 匹配质量排序）"
        else:
            sugar_txt = "/".join(sg) if sg else "未知"
            if len(sg) == 1:
                if sg[0] in composition_allowed:
                    sugar_txt += "（参考库命中且与单糖组成相容；仍需同方法 RI/标准品确认）"
                else:
                    sugar_txt += "（仅参考库命中；库覆盖不足以单独定种）"
                missing.append("SUGAR_SPECIES_EVIDENCE")
                next_steps.append("RUN_SAME_METHOD_STANDARD")
            elif len(sg) > 1:
                sugar_txt += "（EI 不可分，按完整 O-甲基模式排序）"
    ring_txt = (top_residues[0].ring_form
                if (ring_evidenced and top_residues)
                else "unknown（需 NMR 或第二柱复核）")
    linkage_text = (f"{link_str} 连接己糖残基" if sg else f"{link_str} 连接残基")
    seg2 = InterpretationSegment(
        tier=_TIER[identity.value], level=mapping.value,
        text=f"{linkage_text}（{link_str} 位取代）；糖种 {sugar_txt}；环型 {ring_txt}",
        gated=(mapping == ResidueMappingLevel.ambiguous),
        detail={"linkage_positions": [list(lp) for lp in link_sets],
                "sugar_group": sg, "sugar_ranked": sugar_ranked,
                "ring_form": ring_txt, "ring_evidenced": ring_evidenced,
                "species_evidence": species_evidence})

    # 第3段：多糖序列/重复单元（恒 gated，修改方案 P7）
    seg3 = InterpretationSegment(
        tier="未确认", level="unknown", gated=True,
        text="待 NMR(HMBC/NOESY)+MS-MS+酶解，未确认——缺序列/支化分布/重复单元证据，"
             "不得呈现无证据的完整结构（修改方案 P7）。",
        detail={})

    interpretation = Interpretation(pmaa_identity=seg1, residue_linkage=seg2,
                                   sequence=seg3, confidence_tier=_TIER[identity.value])

    return ComponentResult(
        analysis_id=ctx.analysis_id,
        component_id=comp.component_id,
        input_hash=_component_input_hash(comp),
        method_version=ctx.method_id or "unknown_method",
        library_version=ctx.library_version,
        rule_version=RULE_VERSION,
        qc_status=ctx.qc_status,
        derivative_identity_level=identity,
        residue_mapping_level=mapping,
        quantification_level=quant.mode,
        review_status=ReviewStatus.automated,
        candidate_derivatives=candidates,
        candidate_residues=residue_candidates,
        positive_evidence=positive_evidence,
        conflicts=conflicts,
        missing_evidence=sorted(set(missing)),
        match_scores=match_scores,
        quantification=quant,
        next_discriminating_evidence=sorted(set(next_steps)),
        ambiguities=[a.model_dump() for a in ambiguities],
        notes=notes,
        interpretation=interpretation,
        sugar_species=sugar_species,
        species_evidence=species_evidence,
        provenance=comp.provenance,
        extraction_params=comp.extraction_params,
    )


def _component_input_hash(comp: ComponentInput) -> str:
    import hashlib
    import json

    payload = {
        "component_id": comp.component_id,
        "evidence_type": comp.evidence_type.value,
        "ions": {str(k): str(v) for k, v in sorted(comp.ions.items(), key=lambda kv: float(kv[0]))},
        "rt": comp.rt, "area": comp.area,
        "retention_index": comp.retention_index, "retention_time": comp.retention_time,
        "spectrum_completeness": comp.spectrum_completeness.value,
        "provenance": comp.provenance,
        "extraction_params": comp.extraction_params,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
