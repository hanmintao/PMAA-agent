"""领域枚举与稳定错误码。错误码进入 API 响应，前端按码展示文案。"""
from __future__ import annotations

from enum import Enum


class QCStatus(str, Enum):
    passed = "passed"
    warning = "warning"
    failed = "failed"
    not_assessed = "not_assessed"


class DerivativeIdentityLevel(str, Enum):
    """仅描述 PMAA 衍生物身份，不自动等价于原残基唯一。"""
    confirmed = "confirmed"
    probable = "probable"
    candidate = "candidate"
    unknown = "unknown"


class ResidueMappingLevel(str, Enum):
    resolved = "resolved"
    ambiguous = "ambiguous"
    unsupported = "unsupported"


class QuantificationLevel(str, Enum):
    calibrated_molar = "calibrated_molar"
    semiquantitative = "semiquantitative"
    area_only = "area_only"
    unavailable = "unavailable"


class ReviewStatus(str, Enum):
    automated = "automated"
    human_reviewed = "human_reviewed"


class EvidenceType(str, Enum):
    qualitative_ion_list = "qualitative_ion_list"
    numeric_spectrum = "numeric_spectrum"


class SpectrumCompleteness(str, Enum):
    full_acquired_range = "full_acquired_range"
    selected_ions = "selected_ions"
    unknown = "unknown"


class IonObservation(str, Enum):
    present = "present"
    not_detected = "not_detected"
    not_reported = "not_reported"


class IntensityClass(str, Enum):
    """原表颜色语义：标红 = 原表记载相对强度 > 20%（不是我们算出来的强度）。"""
    reported_gt20pct = "reported_gt20pct"
    reported_present = "reported_present"
    numeric = "numeric"
    unknown = "unknown"


class ReferenceReviewStatus(str, Enum):
    unreviewed = "unreviewed"
    quarantined = "quarantined"
    reference_only = "reference_only"
    locally_validated = "locally_validated"


class EvidenceTier(str, Enum):
    E1 = "E1"
    E2 = "E2"
    E3 = "E3"
    E4 = "E4"
    unreviewed = "unreviewed"


class StableErrorCode(str, Enum):
    """稳定错误码（开发计划第 6 节要求）。"""
    LABEL_UNKNOWN = "LABEL_UNKNOWN"
    RETENTION_METHOD_MISMATCH = "RETENTION_METHOD_MISMATCH"
    COMPOSITION_CONFLICT = "COMPOSITION_CONFLICT"
    IDENTICAL_DERIVATIVE_ORIGIN = "IDENTICAL_DERIVATIVE_ORIGIN"
    QUANT_CALIBRATION_MISSING = "QUANT_CALIBRATION_MISSING"
    QC_FAILED = "QC_FAILED"
    METHYL_POSITION_INCOMPATIBLE_WITH_FRAMEWORK = "METHYL_POSITION_INCOMPATIBLE_WITH_FRAMEWORK"
    N_METHYL_POSITION_CONFLICT = "N_METHYL_POSITION_CONFLICT"
    AMINO_SUGAR_BRANCH_UNVALIDATED = "AMINO_SUGAR_BRANCH_UNVALIDATED"
    NUMERIC_INTENSITY_FORBIDDEN = "NUMERIC_INTENSITY_FORBIDDEN"
    UNLISTED_ION_NOT_DETECTED = "UNLISTED_ION_NOT_DETECTED"
    SOURCE_LINKAGE_CONFLICT = "SOURCE_LINKAGE_CONFLICT"
    REPORT_BLOCKED = "REPORT_BLOCKED"
    LLM_UNKNOWN_EVIDENCE = "LLM_UNKNOWN_EVIDENCE"
    LLM_DROPPED_AMBIGUITY = "LLM_DROPPED_AMBIGUITY"
    LLM_UNSUPPORTED_ASSERTION = "LLM_UNSUPPORTED_ASSERTION"
    LLM_PROVIDER_ERROR = "LLM_PROVIDER_ERROR"
    LLM_BUDGET_EXCEEDED = "LLM_BUDGET_EXCEEDED"
    IMPORT_ROW_ISOLATED = "IMPORT_ROW_ISOLATED"
    LOW_LIBRARY_MATCH = "LOW_LIBRARY_MATCH"
