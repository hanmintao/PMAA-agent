"""API 请求/响应模型。严格 extra=forbid，未知字段立即报错而不是静默忽略。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------------ 导入
class ImportPreviewRequest(Strict):
    path: str = Field(description="服务端可见的文件路径（已上传或已挂载）")
    source_title: str = "PMAA EI 汇总表"
    source_url: str | None = None
    evidence_tier: Literal["E1", "E2", "E3", "E4", "unreviewed"] = "E2"
    redistribution_status: Literal["permitted", "restricted", "unknown"] = "restricted"
    method_id: str | None = None


class ImportCommitRequest(ImportPreviewRequest):
    reviewer: str = "system"


# ------------------------------------------------------------------ 样本 / 方法
class SampleCreate(Strict):
    sample_id: str | None = None
    code: str
    source: str | None = None
    batch: str | None = None
    dry_mass_mg: float | None = None
    composition: dict = Field(default_factory=dict)
    modifications: dict = Field(default_factory=dict)
    branch: str = "neutral_aldose"


class PreparationBatchCreate(Strict):
    preparation_batch_id: str | None = None
    batch_code: str
    sop_version: str
    independent_preparation_count: int = Field(default=1, ge=1)
    post_methylation_aliquots: int = Field(default=1, ge=1)
    steps: dict[str, dict] = Field(
        default_factory=dict,
        description="N1–N10 实际记录；每步可含 status、actual_values、reagent_lots、notes")

    @field_validator("steps")
    @classmethod
    def _only_n_steps(cls, value: dict[str, dict]):
        allowed = {f"N{i}" for i in range(1, 11)}
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise ValueError(f"仅允许 N1–N10 步骤代码，发现: {unknown}")
        return value


class MethodCreate(Strict):
    method_id: str
    version: int = 1
    column: str | None = None
    temperature_program: str | None = None
    carrier_gas: str | None = None
    ei_energy_ev: float | None = None
    scan_range_mz: list[float] | None = None
    reduction_route: str | None = None
    payload: dict = Field(default_factory=dict)


class RetentionCreate(Strict):
    derivative_id: str
    method_id: str
    kind: Literal["RT", "RRT", "RI"]
    value: float
    unit: Literal["min", "s", "dimensionless", "index"]
    reference_identity: str | None = None
    algorithm: str | None = None
    n_replicates: int | None = None
    sd: float | None = None


# ------------------------------------------------------------------ 分析
class ComponentInputModel(Strict):
    component_id: str
    peak_label: str | None = None
    rt: float | None = None
    area: float | None = None
    area_is: float | None = None
    evidence_type: Literal["qualitative_ion_list", "numeric_spectrum"]
    ions: list[dict] = Field(default_factory=list,
                             description="定性：[{mz, marker:+|red}]；数值：[{mz, intensity}]")
    spectrum_completeness: Literal["full_acquired_range", "selected_ions", "unknown"] = "unknown"
    detector: str = "EI_TIC"
    integration_method: str = "tic"
    unresolved_group_id: str | None = None
    provenance: Literal["observed", "derived_from_image", "derived_from_document"] = "observed"
    extraction_params: dict | None = None
    # 保留指数（RI）定种通道输入：给 observed RI（推荐）或 observed RT；
    # 缺省则 RI 通道静默（no-op），不改变现有结论。
    retention_index: float | None = None
    retention_time: float | None = None


class RIStandardCreate(Strict):
    """一条标准品保留指数记录；POST 后写入 data/ri_standards.json 即可生效。"""
    method_id: str
    skeleton: str
    o_methyl_positions: list[int] = Field(default_factory=list)
    parent_sugar: str
    retention_index: float | None = None
    retention_time: float | None = None
    source: str = ""


class AnalysisCreate(Strict):
    analysis_id: str | None = None
    sample_id: str | None = None
    preparation_batch_id: str | None = None
    method_id: str | None = None
    idempotency_key: str | None = None
    sample_reduction_label: Literal["NaBD4", "NaBH4", "unknown"] = "unknown"
    composition_detected: list[str] = Field(default_factory=list)
    composition_detection_limit_known: bool = False
    bridge_validated: bool = False
    qc_status: Literal["passed", "warning", "failed", "not_assessed"] = "not_assessed"
    top_k: int = 5
    components: list[ComponentInputModel]


class ReviewSubmit(Strict):
    component_id: str
    decision: dict
    reviewer: str
    reason: str = ""


# ------------------------------------------------------------------ LLM
class ProviderCreate(Strict):
    provider_id: str | None = None
    name: str
    adapter: Literal["mock", "openai_chat", "openai_responses"]
    base_url: str | None = None
    model: str | None = None
    secret_ref: str | None = None
    config: dict = Field(default_factory=dict)
    enabled: bool = True


class AssessRequest(Strict):
    analysis_revision: int = 1
    component_ids: list[str]
    provider_config_id: str
    purpose: Literal["candidate_review"] = "candidate_review"
    idempotency_key: str | None = None
    force_new: bool = False


class AssessmentReview(Strict):
    decision: Literal["accepted", "partially_accepted", "rejected"]
    reviewer: str
    reason: str = ""


# ------------------------------------------------------------------ 报告
class ReportExport(Strict):
    allow_draft_on_failure: bool = True
    fmt: Literal["json", "html"] = "json"
