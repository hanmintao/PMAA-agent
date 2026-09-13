"""分析与复核端点：/v1/analyses、/v1/samples、/v1/methods、/v1/reports。"""
from __future__ import annotations

import hashlib
import json

from fastapi import APIRouter, HTTPException

from ..analysis import qc as qcmod
from ..analysis.pipeline import AnalysisContext, ComponentInput, analyze_component
from ..config import RULE_VERSION
from ..chemistry.retention_index import (
    DEFAULT_RI_TABLE_PATH,
    RetentionStandard,
    load_ri_standards,
)
from ..data import repositories as repo
from ..data.repositories import new_id
from ..domain.enums import EvidenceType, QCStatus, SpectrumCompleteness
from ..domain.errors import PlatformError
from ..reporting import report as reporting
from .schemas import (
    AnalysisCreate,
    ComponentInputModel,
    MethodCreate,
    PreparationBatchCreate,
    RIStandardCreate,
    RetentionCreate,
    ReportExport,
    ReviewSubmit,
    SampleCreate,
)

router = APIRouter()


def _raise(e: PlatformError):
    raise HTTPException(e.http_status, e.to_dict())


# ------------------------------------------------------------------ 样本
@router.post("/samples")
def create_sample(req: SampleCreate):
    sid = req.sample_id or new_id("sample")
    repo.SampleRepo.upsert(sid, code=req.code, source=req.source, batch=req.batch,
                           dry_mass_mg=req.dry_mass_mg, composition=req.composition,
                           modifications=req.modifications, branch=req.branch)
    return {"sample_id": sid}


@router.get("/samples")
def list_samples():
    return {"items": [{"sample_id": s.id, "code": s.code, "batch": s.batch,
                       "branch": s.branch, "composition": s.composition}
                      for s in repo.SampleRepo.list_all()]}


@router.post("/samples/{sample_id}/preparations")
def create_preparation(sample_id: str, req: PreparationBatchCreate):
    """登记 N1–N10 的实际执行记录；计划参数与实际记录不得混为一体。"""
    try:
        repo.SampleRepo.get(sample_id)
    except PlatformError as e:
        _raise(e)
    bid = req.preparation_batch_id or new_id("prep")
    row = repo.PreparationRepo.upsert(
        bid, sample_id=sample_id, batch_code=req.batch_code,
        independent_preparation_count=req.independent_preparation_count,
        post_methylation_aliquots=req.post_methylation_aliquots,
        steps={"sop_version": req.sop_version, "records": req.steps})
    repo.audit("preparation_batch", bid, "created", {
        "sample_id": sample_id, "sop_version": req.sop_version,
        "recorded_steps": sorted(req.steps)})
    return {"preparation_batch_id": row.id, "sample_id": row.sample_id,
            "batch_code": row.batch_code, "steps": row.steps}


@router.get("/samples/{sample_id}/preparations")
def list_preparations(sample_id: str):
    rows = repo.PreparationRepo.by_sample(sample_id)
    return {"items": [{
        "preparation_batch_id": r.id, "sample_id": r.sample_id,
        "batch_code": r.batch_code,
        "independent_preparation_count": r.independent_preparation_count,
        "post_methylation_aliquots": r.post_methylation_aliquots,
        "steps": r.steps,
    } for r in rows]}


# ------------------------------------------------------------------ 方法
@router.post("/methods")
def create_method(req: MethodCreate):
    row = repo.MethodRepo.upsert(req.method_id, version=req.version, column=req.column,
                                 temperature_program=req.temperature_program,
                                 carrier_gas=req.carrier_gas, ei_energy_ev=req.ei_energy_ev,
                                 scan_range_mz=req.scan_range_mz,
                                 reduction_route=req.reduction_route, payload=req.payload)
    return {"method_id": row.id, "version": row.version}


@router.get("/methods")
def list_methods():
    return {"items": [{"method_id": m.id, "version": m.version, "column": m.column,
                       "locked": m.locked} for m in repo.MethodRepo.list_all()]}


@router.post("/methods/{method_id}/retention")
def add_retention(method_id: str, req: RetentionCreate):
    row = repo.RetentionRepo.add(
        req.derivative_id, method_id=req.method_id, kind=req.kind, value=req.value,
        unit=req.unit, reference_identity=req.reference_identity, algorithm=req.algorithm,
        n_replicates=req.n_replicates, sd=req.sd)
    return {"id": row.id}


# ------------------------------------------------------------------ RI 标准库（保留指数定种通道）
@router.get("/ri-standards")
def list_ri_standards():
    """读取当前 RI 标准表；文件缺失/空返回空列表，RI 通道静默（no-op）。"""
    return {"items": [s.__dict__ for s in load_ri_standards()]}


@router.post("/ri-standards")
def add_ri_standard(req: RIStandardCreate):
    """追加一条 RI 标准；写入 data/ri_standards.json 后立即生效（数据驱动，无需改逻辑）。"""
    import json as _json
    path = DEFAULT_RI_TABLE_PATH
    table = load_ri_standards(path)
    key = (req.method_id, req.skeleton, tuple(req.o_methyl_positions), req.parent_sugar)
    new_std = RetentionStandard(
        method_id=req.method_id, skeleton=req.skeleton,
        o_methyl_positions=tuple(req.o_methyl_positions), parent_sugar=req.parent_sugar,
        retention_index=req.retention_index, retention_time=req.retention_time,
        source=req.source)
    # 同 (method_id, skeleton, 甲基化位点, 糖种) 视为同一标准 → 覆盖更新
    kept = [s for s in table
            if (s.method_id, s.skeleton, s.o_methyl_positions, s.parent_sugar) != key]
    kept.append(new_std)
    path.write_text(_json.dumps([s.__dict__ for s in kept], ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return {"status": "ok", "count": len(kept), "standard": new_std.__dict__}


# ------------------------------------------------------------------ 分析
def _component_input(m: ComponentInputModel) -> ComponentInput:
    ions: dict[float, object] = {}
    for ion in m.ions:
        mz = float(ion["mz"])
        if m.evidence_type == "numeric_spectrum":
            ions[mz] = float(ion["intensity"])
        else:
            ions[mz] = str(ion.get("marker", "+"))
    return ComponentInput(
        component_id=m.component_id,
        evidence_type=EvidenceType(m.evidence_type),
        ions=ions, peak_label=m.peak_label, rt=m.rt, area=m.area, area_is=m.area_is,
        spectrum_completeness=SpectrumCompleteness(m.spectrum_completeness),
        detector=m.detector, integration_method=m.integration_method,
        unresolved_group_id=m.unresolved_group_id,
        provenance=m.provenance, extraction_params=m.extraction_params,
        retention_index=m.retention_index, retention_time=m.retention_time,
    )


@router.post("/analyses")
def create_analysis(req: AnalysisCreate):
    """创建分析任务并执行规则/谱库候选计算（同步执行；长任务接口形态保持一致）。"""
    if req.idempotency_key:
        existing = repo.AnalysisRepo.by_idempotency_key(req.idempotency_key)
        if existing:
            return {"analysis_id": existing.id, "status": "reused", "revision": existing.revision}

    if req.preparation_batch_id:
        try:
            prep = repo.PreparationRepo.get(req.preparation_batch_id)
        except PlatformError as e:
            _raise(e)
        if req.sample_id and prep.sample_id != req.sample_id:
            raise HTTPException(422, {
                "error_code": "PREPARATION_SAMPLE_MISMATCH",
                "message": "制备批次不属于本次分析所选样本",
                "details": {"preparation_batch_id": prep.id, "sample_id": req.sample_id},
            })

    payload = req.model_dump()
    input_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()

    aid = req.analysis_id or new_id("analysis")
    ctx = AnalysisContext(
        analysis_id=aid, method_id=req.method_id,
        sample_reduction_label=req.sample_reduction_label,
        composition_detected=set(req.composition_detected),
        composition_detection_limit_known=req.composition_detection_limit_known,
        bridge_validated=req.bridge_validated, qc_status=QCStatus(req.qc_status),
        top_k=req.top_k)

    repo.AnalysisRepo.create(
        aid, sample_id=req.sample_id, method_id=req.method_id,
        library_version="library_v1", rule_version=RULE_VERSION,
        input_hash=input_hash, revision=1, status="running",
        qc_status=req.qc_status, idempotency_key=req.idempotency_key, payload=payload)

    results = []
    for m in req.components:
        comp = _component_input(m)
        try:
            res = analyze_component(comp, ctx)
        except PlatformError as e:
            _raise(e)
        row = repo.ComponentRepo.upsert(
            f"{aid}::{comp.component_id}", analysis_id=aid, component_id=comp.component_id,
            peak_label=comp.peak_label, rt=comp.rt, area=comp.area,
            evidence_type=comp.evidence_type.value,
            spectrum_completeness=comp.spectrum_completeness.value,
            ions=m.model_dump()["ions"], input_hash=res.input_hash,
            result=res.model_dump(mode="json"))
        results.append({"component_id": comp.component_id, "result": res.model_dump(mode="json")})

    repo.AnalysisRepo.update(aid, status="succeeded")
    repo.audit("analysis", aid, "created", {"input_hash": input_hash, "components": len(results)})
    return {"analysis_id": aid, "status": "succeeded", "revision": 1,
            "input_hash": input_hash, "components": results}


@router.get("/analyses/{analysis_id}/components")
def list_components(analysis_id: str):
    try:
        rows = repo.ComponentRepo.by_analysis(analysis_id)
    except PlatformError as e:
        _raise(e)
    return {"analysis_id": analysis_id, "items": [{
        "component_id": r.component_id, "peak_label": r.peak_label, "rt": r.rt,
        "area": r.area, "evidence_type": r.evidence_type, "result": r.result,
    } for r in rows]}


@router.get("/analyses")
def list_analyses():
    return {"items": [{"analysis_id": a.id, "status": a.status, "qc_status": a.qc_status,
                       "revision": a.revision, "input_hash": a.input_hash,
                       "created_at": a.created_at.isoformat()} for a in repo.AnalysisRepo.list_all()]}


@router.post("/analyses/{analysis_id}/reviews")
def submit_review(analysis_id: str, req: ReviewSubmit):
    """人工复核：覆盖判断保留前值，必须给出证据引用与原因。"""
    if not req.reason:
        raise HTTPException(422, {"error_code": "REVIEW_REASON_REQUIRED",
                                  "message": "复核必须填写原因"})
    component_row_id = f"{analysis_id}::{req.component_id}"
    try:
        repo.ComponentRepo.get(component_row_id)
    except PlatformError as e:
        _raise(e)
    prior = repo.DecisionRepo.by_component(component_row_id)
    if "confirmed" in json.dumps(req.decision, ensure_ascii=False) and \
            not req.decision.get("evidence_ids"):
        raise HTTPException(422, {"error_code": "REVIEW_EVIDENCE_REQUIRED",
                                  "message": "确认结论必须引用证据，不能以勾选代替证据门槛"})
    row = repo.DecisionRepo.add(
        component_row_id, decision=req.decision, reviewer=req.reviewer, reason=req.reason,
        supersedes_id=prior[-1].id if prior else None)
    return {"decision_id": row.id, "supersedes": row.supersedes_id}


@router.get("/analyses/{analysis_id}/qc")
def get_qc(analysis_id: str):
    a = repo.AnalysisRepo.get(analysis_id)
    return {"analysis_id": analysis_id, "qc_status": a.qc_status,
            "payload": a.payload.get("qc") if a.payload else None}


@router.post("/analyses/{analysis_id}/qc")
def evaluate_qc(analysis_id: str, payload: dict):
    """按实验记录计算样本级 QC；失败即冻结正式放行（A18）。"""
    res = qcmod.evaluate_batch_qc(
        blank_peak_ratio=payload.get("blank_peak_ratio"),
        independent_preparations=payload.get("independent_preparations", 1),
        post_methylation_aliquots=payload.get("post_methylation_aliquots", 1),
        repeat_cv=payload.get("repeat_cv"),
        recovery=payload.get("recovery"),
        remethylation_shift_exceeds_repeatability=payload.get(
            "remethylation_shift_exceeds_repeatability", False),
        unresolved_group_count=payload.get("unresolved_group_count", 0),
        unidentified_area_ratio=payload.get("unidentified_area_ratio"))
    analysis = repo.AnalysisRepo.get(analysis_id)
    analysis_payload = dict(analysis.payload or {})
    analysis_payload["qc"] = {
        "inputs": payload,
        "status": res.status.value,
        "formal_report_allowed": res.formal_report_allowed,
        "blockers": res.blockers,
        "checks": [c.__dict__ for c in res.checks],
    }
    repo.AnalysisRepo.update(analysis_id, qc_status=res.status.value, payload=analysis_payload)
    repo.audit("analysis", analysis_id, "qc_evaluated", {"status": res.status.value})
    return {"analysis_id": analysis_id, "qc_status": res.status.value,
            "formal_report_allowed": res.formal_report_allowed,
            "blockers": res.blockers,
            "checks": [c.__dict__ for c in res.checks]}


# ------------------------------------------------------------------ 报告
@router.post("/analyses/{analysis_id}/reports")
def export_report(analysis_id: str, req: ReportExport):
    try:
        payload = reporting.build_report(analysis_id,
                                         allow_draft_on_failure=req.allow_draft_on_failure)
        saved = reporting.save_report(payload)
    except PlatformError as e:
        _raise(e)
    if req.fmt == "html":
        from fastapi.responses import HTMLResponse
        return HTMLResponse(reporting.render_html(saved))
    return saved


@router.get("/reports/{analysis_id}")
def list_reports(analysis_id: str):
    return {"items": [{"report_id": r.id, "kind": r.kind, "revision": r.analysis_revision,
                       "sha256": r.sha256, "created_at": r.created_at.isoformat()}
                      for r in repo.ReportRepo.list_for_analysis(analysis_id)]}


@router.get("/reports/by-id/{report_id}")
def get_report(report_id: str):
    """读取已经冻结的历史报告，不触发重新分析或 LLM 调用。"""
    try:
        return repo.ReportRepo.get(report_id).payload
    except PlatformError as e:
        _raise(e)
