"""
检索门面：把数据库中的参考记录装配成可检索索引。

定性库与定量库严格分开——同一条记录若只有离子集合，绝不进入数值谱索引。
"""
from __future__ import annotations

from ..data import repositories as repo
from ..domain.enums import EvidenceType, IntensityClass, IonObservation
from .qualitative import QualitativeIndex, RefEntry


def load_library(*, include_quarantined: bool = True) -> tuple[QualitativeIndex, dict[str, RefEntry]]:
    """
    :return: (索引, {record_id: RefEntry})
    隔离条目（quarantined）默认仍进入检索——规则要求保留候选供研究，
    但调用方必须依据 entry.meta["review_status"] 限制其证据上限。
    """
    rows = repo.ReferenceRepo.list_all(include_quarantined=include_quarantined)
    entries: list[RefEntry] = []
    by_id: dict[str, RefEntry] = {}
    for r in rows:
        ions: dict[float, IntensityClass] = {}
        for ion in r.ions:
            if ion.observation != IonObservation.present.value:
                continue
            ions[ion.mz] = IntensityClass(ion.intensity_class)
        if not ions:
            continue
        e = RefEntry(
            record_id=r.id, ions=ions,
            meta={
                "derivative_name_raw": r.derivative_name_raw,
                "parent_sugar": r.parent_sugar,
                "skeleton": r.skeleton,
                "ring_form": r.ring_form,
                "o_methyl_positions": r.o_methyl_positions,
                "o_acetyl_positions": r.o_acetyl_positions,
                "nitrogen_modifications": r.nitrogen_modifications,
                "review_status": r.review_status,
                "branch": r.branch,
                "evidence_type": r.evidence_type,
                "source_row_id": r.source_row_id,
                "source_locator": (r.source or {}).get("locator"),
                "linkage_conflict": r.linkage_conflict,
                "reduction_label": r.reduction_label,
                "derived_linkage": r.derived_linkage,
            },
        )
        entries.append(e)
        by_id[r.id] = e
    return QualitativeIndex(entries), by_id


def load_numeric_library() -> dict[str, dict[float, float]]:
    """只有 evidence_type == numeric_spectrum 且强度非空的记录才进数值库。"""
    rows = repo.ReferenceRepo.list_all()
    out: dict[str, dict[float, float]] = {}
    for r in rows:
        if r.evidence_type != EvidenceType.numeric_spectrum.value:
            continue
        spec = {ion.mz: ion.relative_intensity for ion in r.ions
                if ion.relative_intensity is not None}
        if spec:
            out[r.id] = spec
    return out
