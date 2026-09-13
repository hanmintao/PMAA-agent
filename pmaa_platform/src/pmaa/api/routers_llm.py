"""LLM 网关端点（M11 / M13）：/v1/llm/providers、/v1/analyses/{id}/llm-assessments。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..analysis.evidence import build_bundle
from ..config import LLM_RUNTIME_DEFAULTS, RULE_VERSION
from ..data import repositories as repo
from ..data.database import new_id
from ..domain.enums import StableErrorCode
from ..domain.errors import LLMError, PlatformError
from ..domain.models import EvidenceBundle
from ..llm import gateway
from ..llm.adapters import build_adapter
from .schemas import AssessmentReview, AssessRequest, ProviderCreate

router = APIRouter()


def _raise(e: PlatformError):
    raise HTTPException(e.http_status, e.to_dict())


def _mask(cfg) -> dict:
    """返回脱敏配置：密钥只给 secret_ref，绝不回传明文。"""
    return {
        "provider_id": cfg.id, "name": cfg.name, "adapter": cfg.adapter,
        "base_url": cfg.base_url, "model": cfg.model,
        "secret_ref": cfg.secret_ref, "config_version": cfg.config_version,
        "enabled": cfg.enabled,
        "key_present": bool(_key_env_present(cfg.secret_ref)),
    }


def _key_env_present(secret_ref: str | None) -> bool:
    import os
    return bool(secret_ref and os.environ.get(secret_ref.upper()))


@router.post("/llm/providers")
def create_provider(req: ProviderCreate):
    pid = req.provider_id or new_id("provider")
    row = repo.LLMProviderRepo.upsert(
        pid, name=req.name, adapter=req.adapter, base_url=req.base_url, model=req.model,
        secret_ref=req.secret_ref, config=req.config, enabled=req.enabled)
    repo.audit("llm_provider", pid, "created", {"adapter": req.adapter})
    return _mask(row)


@router.get("/llm/providers")
def list_providers():
    return {"items": [_mask(p) for p in repo.LLMProviderRepo.list_all()]}


@router.post("/llm/providers/{provider_id}/test")
def test_provider(provider_id: str):
    """无样本连接与能力测试：不发任何实验数据（13.2）。"""
    try:
        cfg = repo.LLMProviderRepo.get(provider_id)
        adapter = build_adapter(cfg.adapter, base_url=cfg.base_url or "", model=cfg.model or "",
                                secret_ref=cfg.secret_ref, timeout=10)
    except PlatformError as e:
        _raise(e)
    caps = adapter.capabilities()
    # mock 适配器离线可用；HTTP 适配器只做配置完整性检查，不消耗样本额度
    result = {
        "provider_id": provider_id,
        "adapter": cfg.adapter,
        "capabilities": caps,
        "structured_output": caps.get("structured_output"),
        "key_present": _key_env_present(cfg.secret_ref),
        "runtime_defaults": LLM_RUNTIME_DEFAULTS,
        "note": "本测试不发送任何实验数据；真实能力需在接入时逐项探测。",
    }
    if cfg.adapter != "mock" and not _key_env_present(cfg.secret_ref):
        result["warning"] = f"环境变量 {cfg.secret_ref.upper() if cfg.secret_ref else '?'} 未设置，调用将失败"
    return result


@router.post("/llm/providers/{provider_id}/config")
def update_provider(provider_id: str, payload: dict):
    """修改配置会使 config_version 自增，从而使既有缓存键失效（B12）。"""
    try:
        cfg = repo.LLMProviderRepo.get(provider_id)
    except PlatformError as e:
        _raise(e)
    allowed = {"name", "adapter", "base_url", "model", "secret_ref", "config", "enabled"}
    kw = {k: v for k, v in payload.items() if k in allowed}
    kw["config_version"] = cfg.config_version + 1
    row = repo.LLMProviderRepo.upsert(provider_id, **kw)
    repo.audit("llm_provider", provider_id, "config_updated",
               {"config_version": row.config_version})
    return _mask(row)


@router.post("/analyses/{analysis_id}/llm-assessments")
def assess(analysis_id: str, req: AssessRequest):
    """
    请求 LLM 辅助复核。证据包由服务端构建，客户端无法塞入未经验证的标签。
    返回 202 语义：成功创建评议任务/结果（本实现同步完成，保留 job 形态字段）。
    """
    try:
        analysis = repo.AnalysisRepo.get(analysis_id)
    except PlatformError as e:
        _raise(e)

    components = {c.component_id: c for c in repo.ComponentRepo.by_analysis(analysis_id)}
    out = []
    for cid in req.component_ids:
        crow = components.get(cid)
        if crow is None:
            out.append({"component_id": cid, "status": "failed",
                        "error_code": "COMPONENT_NOT_FOUND",
                        "message": f"组分 {cid} 不在分析 {analysis_id} 中"})
            continue
        result = crow.result or {}
        try:
            experimental_context = None
            prep_id = (analysis.payload or {}).get("preparation_batch_id")
            if prep_id:
                prep = repo.PreparationRepo.get(prep_id)
                experimental_context = {
                    "preparation_batch_id": prep.id,
                    "independent_preparation_count": prep.independent_preparation_count,
                    "post_methylation_aliquots": prep.post_methylation_aliquots,
                    "records": (prep.steps or {}).get("records", {}),
                }
            bundle = build_bundle(
                analysis_id=analysis_id, analysis_revision=req.analysis_revision,
                component_id=cid, result=result, component_row=crow,
                library_version=analysis.library_version, rule_version=RULE_VERSION,
                method_version=analysis.method_id or "unknown_method",
                evidence_type=crow.evidence_type,
                spectrum_completeness=crow.spectrum_completeness,
                input_hash=crow.input_hash, qc_status=analysis.qc_status,
                experimental_context=experimental_context)
            view = gateway.assess_component(
                bundle=bundle, provider_id=req.provider_config_id, purpose=req.purpose,
                idempotency_key=req.idempotency_key, force_new=req.force_new)
        except LLMError as e:
            out.append({"component_id": cid, "status": "failed",
                        "error_code": e.code.value, "message": e.message,
                        "fallback_to_rule_results": True})
            continue
        except PlatformError as e:
            _raise(e)
        view["component_id"] = cid
        view["bundle_id"] = bundle.bundle_id
        out.append(view)
    return {"analysis_id": analysis_id, "analysis_revision": req.analysis_revision,
            "assessments": out}


@router.get("/llm-assessments/{assessment_id}")
def get_assessment(assessment_id: str):
    try:
        row = repo.AssessmentRepo.get(assessment_id)
    except PlatformError as e:
        _raise(e)
    return {
        "assessment_id": row.id, "bundle_id": row.bundle_id, "status": row.status,
        "finish_status": row.finish_status,
        "provider_id": row.provider_id, "provider_config_version": row.provider_config_version,
        "provider_response_model": row.provider_response_model,
        "provider_request_id": row.provider_request_id,
        "prompt_version": row.prompt_version, "prompt_hash": row.prompt_hash,
        "request_hash": row.request_hash, "schema_version": row.schema_version,
        "generation_parameters": row.generation_parameters,
        "raw_response": row.raw_response, "parsed_response": row.parsed_response,
        "usage": row.usage, "validation": row.validation,
        "reviewer_decision": row.reviewer_decision,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@router.post("/llm-assessments/{assessment_id}/reviews")
def review_assessment(assessment_id: str, req: AssessmentReview):
    try:
        out = gateway.record_review(assessment_id, decision=req.decision,
                                    reviewer=req.reviewer, reason=req.reason)
    except PlatformError as e:
        _raise(e)
    return out


@router.get("/analyses/{analysis_id}/llm-assessments")
def list_assessments(analysis_id: str):
    rows = repo.AssessmentRepo.list_for_analysis(analysis_id)
    return {"items": [{"assessment_id": r.id, "status": r.status,
                       "finish_status": r.finish_status,
                       "provider_id": r.provider_id,
                       "validation": r.validation,
                       "reviewer_decision": r.reviewer_decision} for r in rows]}
