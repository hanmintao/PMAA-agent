"""
领域模型（Pydantic v2）。字段与 03_数据契约与验收用例.json 的
reference_record_schema / result_contract 一一对应，additionalProperties 一律禁止，
避免未知字段被静默接受。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .enums import (
    DerivativeIdentityLevel,
    EvidenceTier,
    EvidenceType,
    IntensityClass,
    IonObservation,
    QCStatus,
    QuantificationLevel,
    ReferenceReviewStatus,
    ResidueMappingLevel,
    ReviewStatus,
    SpectrumCompleteness,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


# ------------------------------------------------------------------ 标签
class LabelPosition(StrictModel):
    atom_position: str
    isotope: str
    count: int = Field(ge=1)


class ReductionLabel(StrictModel):
    anomeric_reduction: Literal["NaBD4", "NaBH4", "4-MMB", "other", "unknown"]
    carboxyl_reduction: Literal["none", "NaBD4_activated", "NaBH4_activated", "other", "unknown"]
    methylation_reagent: Literal["CH3I", "CD3I", "other", "unknown"]
    label_positions: list[LabelPosition] = Field(default_factory=list)


# ------------------------------------------------------------------ 离子
class Ion(StrictModel):
    mz: float = Field(gt=0)
    observation: IonObservation
    relative_intensity: float | None = Field(default=None, ge=0, le=100)
    intensity_class: IntensityClass
    source_cell: str | None = None

    @field_validator("relative_intensity")
    @classmethod
    def _qualitative_has_no_intensity(cls, v, info):
        return v  # 跨字段约束在 ReferenceRecord 层统一做


# ------------------------------------------------------------------ 来源 / 方法
class SourceInfo(StrictModel):
    title: str
    url: str | None = None
    file_sha256: str | None = None
    locator: str                       # 例：Sheet1!R3C2
    access_depth: Literal["local_raw", "full_text", "database_record", "abstract_only", "unknown"]
    evidence_tier: EvidenceTier
    redistribution_status: Literal["permitted", "restricted", "unknown"]


class RetentionObservation(StrictModel):
    kind: Literal["RT", "RRT", "RI"]
    value: float = Field(ge=0)
    unit: Literal["min", "s", "dimensionless", "index"]
    method_id: str
    reference_identity: str | None = None
    algorithm: str | None = None

    @field_validator("unit")
    @classmethod
    def _unit_matches_kind(cls, v, info):
        kind = info.data.get("kind")
        # 03 契约 A11：RRT 无量纲；RT 必须带时间单位
        if kind == "RT" and v not in ("min", "s"):
            raise ValueError("RRT_UNIT_MISMATCH: RT 必须使用 min 或 s")
        if kind == "RRT" and v != "dimensionless":
            raise ValueError("RRT_UNIT_MISMATCH: RRT 无量纲")
        if kind == "RI" and v != "index":
            raise ValueError("RRT_UNIT_MISMATCH: RI 单位必须为 index")
        return v


class MethodSnapshot(StrictModel):
    method_id: str | None = None
    ei_energy_ev: float | None = Field(default=None, gt=0)
    scan_range_mz: list[float] | None = None
    retention: RetentionObservation | None = None


# ------------------------------------------------------------------ 参考记录
class ReferenceRecord(StrictModel):
    record_id: str = Field(min_length=1)
    derivative_name_raw: str = Field(min_length=1)
    parent_sugar: str | None = None
    ring_form: Literal["p", "f", "unknown"] | None = None
    o_methyl_positions: list[int] = Field(default_factory=list)
    o_acetyl_positions: list[int] | None = None
    nitrogen_modifications: list[str] = Field(default_factory=list)
    reduction_label: ReductionLabel
    evidence_type: EvidenceType
    spectrum_completeness: SpectrumCompleteness
    ions: list[Ion] = Field(min_length=1)
    source: SourceInfo
    method: MethodSnapshot
    review_status: ReferenceReviewStatus

    # 平台扩展（不违背契约：属于导入审计字段，非化学字段）
    skeleton: str | None = None
    source_row_id: str | None = None
    source_linkage_raw: str | None = None
    derived_linkage: list[int] = Field(default_factory=list)
    linkage_conflict: bool = False
    branch: str = "neutral_aldose"
    quarantine_reason: str | None = None

    @field_validator("o_methyl_positions", "o_acetyl_positions")
    @classmethod
    def _in_range(cls, v):
        if v is None:
            return v
        for p in v:
            if not 1 <= p <= 9:
                raise ValueError("POSITION_OUT_OF_RANGE")
        return v

    @field_validator("ions")
    @classmethod
    def _no_duplicate_mz(cls, v: list[Ion]):
        seen = set()
        for ion in v:
            if ion.mz in seen:
                raise ValueError("DUPLICATE_MZ_IN_RECORD")
            seen.add(ion.mz)
        return v

    @field_validator("ions")
    @classmethod
    def _qualitative_must_not_carry_intensity(cls, v: list[Ion], info):
        et = info.data.get("evidence_type")
        if et == EvidenceType.qualitative_ion_list:
            for ion in v:
                if ion.relative_intensity is not None:
                    # 03 契约 A01：不得把 "+" 变成强度
                    raise ValueError("NUMERIC_INTENSITY_FORBIDDEN")
        return v


# ------------------------------------------------------------------ 映射 / 候选
class ResidueMapping(StrictModel):
    framework_id: str
    parent_sugar: str | None = None
    ring_form: str
    linkage_positions: list[int]
    precondition: str
    source_record_id: str | None = None


class CandidateDerivative(StrictModel):
    candidate_id: str
    derivative_id: str
    derivative_name: str
    o_methyl_positions: list[int]
    o_acetyl_positions: list[int]
    score: float
    # 分项证据既有数值也有状态字符串（如保留证据状态），故放宽为 Any
    score_components: dict[str, Any] = Field(default_factory=dict)
    identity_level: DerivativeIdentityLevel
    supported_by: list[str] = Field(default_factory=list)
    # 评分透明度（修改方案 P1/P3）：把 coverage/红峰/低覆盖提示直接透出，前端可展示
    coverage: float | None = None
    precision: float | None = None
    red_matched: int | None = None
    red_total: int | None = None
    low_coverage_warning: bool = False


class CandidateResidue(StrictModel):
    derivative_id: str           # 明确归属哪个 PMAA，避免多候选时形成错误笛卡尔积
    residue_label: str            # 例 "4-Hexp"
    framework_id: str
    ring_form: str
    linkage_positions: list[int]
    mapping_level: ResidueMappingLevel
    parent_sugar: str | None = None


# ------------------------------------------------------------------ 三段式判读（修改方案 P7 / 4.7）
class InterpretationSegment(StrictModel):
    """三段式判读中的一段：明确分层，避免从 PMAA 直接跳到“完整结构”。"""
    tier: str                      # 置信度分级（修改方案 P6）：候选 / 支持 / 确认
    level: str                     # 底层枚举值：unknown / candidate / probable / confirmed
    text: str                      # 该段结论文本
    gated: bool = False            # True = 被证据门控，等待更多证据（第3段恒为 True）
    detail: dict[str, Any] = Field(default_factory=dict)


class Interpretation(StrictModel):
    pmaa_identity: InterpretationSegment    # 第1段：PMAA 身份（候选/支持）
    residue_linkage: InterpretationSegment  # 第2段：残基连接模式（候选/支持）
    sequence: InterpretationSegment         # 第3段：多糖序列/重复单元（恒 gated 未确认）
    confidence_tier: str                    # 整体置信度：候选 / 支持 / 确认


class Quantification(StrictModel):
    mode: QuantificationLevel
    detector: str
    integration_method: str
    calibration_id: str | None = None
    value: float | None = None
    unit: str | None = None
    denominator_definition: str | None = None
    below_loq: bool = False
    unresolved_group_id: str | None = None


# ------------------------------------------------------------------ 分析结果
class ComponentResult(StrictModel):
    """03 契约 result_contract.required_fields 的完整落地。"""
    analysis_id: str
    component_id: str
    input_hash: str
    method_version: str
    library_version: str
    rule_version: str
    qc_status: QCStatus
    derivative_identity_level: DerivativeIdentityLevel
    residue_mapping_level: ResidueMappingLevel
    quantification_level: QuantificationLevel
    review_status: ReviewStatus
    candidate_derivatives: list[CandidateDerivative]
    candidate_residues: list[CandidateResidue]
    positive_evidence: list[dict[str, Any]]
    conflicts: list[dict[str, Any]]
    missing_evidence: list[str]
    match_scores: dict[str, float]
    quantification: Quantification
    next_discriminating_evidence: list[str]
    ambiguities: list[dict[str, Any]] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    # 三段式判读（修改方案 P7）：PMAA 身份 → 残基连接模式 → 多糖序列/重复单元
    interpretation: "Interpretation | None" = None
    # 保留指数（RI）定种通道结论：EI 不可分糖种，RI 作为正交维度收窄糖种。
    # 无 RI 数据或未提供 observed RI/RT 时恒为 None（no-op，不改变现有行为）。
    sugar_species: str | None = None
    species_evidence: dict[str, Any] | None = None
    # 数据出处：observed / derived_from_image / derived_from_document
    provenance: str = "observed"
    extraction_params: dict[str, Any] | None = None


# ------------------------------------------------------------------ 证据包 / LLM
class Fact(StrictModel):
    fact_id: str
    field: str
    value: Any
    unit: str | None = None
    basis: Literal["observed", "derived", "reference", "unknown"]
    source_locator: str


class RuleItem(StrictModel):
    rule_id: str
    text: str


class Ambiguity(StrictModel):
    ambiguity_id: str
    candidate_ids: list[str]
    type: Literal[
        "identical_derivative_origins", "coelution", "similar_ei", "label_unknown", "other"
    ]
    required_evidence_code: str


class EvidenceCeiling(StrictModel):
    maximum_derivative_identity_level: DerivativeIdentityLevel
    residue_mapping_must_remain_ambiguous: bool
    calibrated_quantification_available: bool


class EvidenceBundle(StrictModel):
    schema_version: str = "1.0"
    bundle_id: str
    analysis_id: str
    analysis_revision: int
    component_id: str
    evidence_type: EvidenceType
    spectrum_completeness: SpectrumCompleteness
    input_hash: str
    method_version: str
    library_version: str
    rule_version: str
    qc_status: QCStatus
    facts: list[Fact]
    candidates: list[dict[str, Any]]
    rules: list[RuleItem]
    ambiguities: list[Ambiguity]
    missing_evidence_codes: list[str]
    evidence_ceiling: EvidenceCeiling


class LLMAssessmentOutput(StrictModel):
    """04 契约 provider_assessment_output。"""
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    component_id: str
    summary: str
    ranking_groups: list[dict[str, Any]]
    candidate_reviews: list[dict[str, Any]]
    retained_ambiguity_ids: list[str]
    missing_evidence_codes: list[str]
    next_actions: list[dict[str, Any]]
    recommended_disposition: Literal["maintain_candidates", "needs_review", "request_additional_evidence"]
