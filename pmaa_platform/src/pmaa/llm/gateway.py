"""
LLM 网关（M11 / 13.7）：超时、重试、缓存、预算与回退。

关键行为：
  * 缓存键包含 项目/租户、analysis_revision、证据包哈希、规则/库版本、
    provider/model 配置版本、提示词与 Schema 版本（13.7 末）；
  * 401/403 不重试；429/5xx 最多 2 次退避重试并遵循总时限；
  * Schema 修复最多 1 次；
  * 任何失败都保留规则结果——模型失败不会使有效实验结果丢失。
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid

from ..config import LLM_RUNTIME_DEFAULTS, LLM_SCHEMA_VERSION
from ..data import repositories as repo
from ..data.database import new_id
from ..domain.enums import StableErrorCode
from ..domain.errors import LLMError, NotFoundError
from ..domain.models import EvidenceBundle
from .adapters import build_adapter
from .prompts import build_user_prompt, prompt_hashes
from .validation import validate_assessment

# 与 04 契约 provider_assessment_output 对齐的输出 Schema（用于结构化输出模式）
OUTPUT_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "component_id", "summary", "ranking_groups",
                 "candidate_reviews", "retained_ambiguity_ids", "missing_evidence_codes",
                 "next_actions", "recommended_disposition"],
    "properties": {
        "schema_version": {"type": "string", "const": LLM_SCHEMA_VERSION},
        "component_id": {"type": "string"},
        "summary": {"type": "string"},
        "ranking_groups": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["candidate_ids"],
            "properties": {"candidate_ids": {"type": "array", "items": {"type": "string"}}}}},
        "candidate_reviews": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["candidate_id", "assessment", "evidence_ids", "rule_ids", "reason_summary"],
            "properties": {
                "candidate_id": {"type": "string"},
                "assessment": {"type": "string", "enum": ["supports", "contradicts", "insufficient"]},
                "evidence_ids": {"type": "array", "items": {"type": "string"}},
                "rule_ids": {"type": "array", "items": {"type": "string"}},
                "reason_summary": {"type": "string"}}}},
        "retained_ambiguity_ids": {"type": "array", "items": {"type": "string"}},
        "missing_evidence_codes": {"type": "array", "items": {"type": "string"}},
        "next_actions": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["action_code", "evidence_ids", "purpose"],
            "properties": {
                "action_code": {"type": "string", "enum": [
                    "REVIEW_NMR_RING_EVIDENCE", "RUN_SAME_METHOD_STANDARD", "RUN_SECOND_COLUMN",
                    "REVIEW_METHYLATION_COMPLETENESS", "REVIEW_HYDROLYSIS_RECOVERY",
                    "VERIFY_REDUCTION_LABEL", "REVIEW_COMPOSITION_METHOD", "OBTAIN_FULL_EI",
                    "REVIEW_CALIBRATION", "REQUEST_LIBRARY_EXPANSION", "MANUAL_REVIEW"]},
                "evidence_ids": {"type": "array", "items": {"type": "string"}},
                "purpose": {"type": "string"}}}},
        "recommended_disposition": {"type": "string",
                                    "enum": ["maintain_candidates", "needs_review",
                                             "request_additional_evidence"]},
    },
}


def request_hash(*, bundle_hash: str, provider_id: str, config_version: int,
                 prompt_hash: str, model: str | None, schema_version: str,
                 generation: dict) -> str:
    raw = json.dumps({"bundle_hash": bundle_hash, "provider_id": provider_id,
                      "config_version": config_version, "prompt_hash": prompt_hash,
                      "model": model, "schema_version": schema_version,
                      "generation": generation}, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


class Budget:
    """简单的按日/按样本费用与次数限制（B10）。"""

    def __init__(self, *, daily_limit: float | None = None, per_sample_limit: float | None = None,
                 max_calls_per_day: int | None = None):
        self.daily_limit = daily_limit
        self.per_sample_limit = per_sample_limit
        self.max_calls_per_day = max_calls_per_day
        self._spent_today = 0.0
        self._calls_today = 0
        self._spent_sample: dict[str, float] = {}

    def check(self, sample_key: str = "default") -> None:
        if self.daily_limit is not None and self._spent_today >= self.daily_limit:
            raise LLMError("已达单日费用上限，LLM 层暂停（基础分析继续）",
                           code=StableErrorCode.LLM_BUDGET_EXCEEDED)
        if self.max_calls_per_day is not None and self._calls_today >= self.max_calls_per_day:
            raise LLMError("已达单日调用次数上限", code=StableErrorCode.LLM_BUDGET_EXCEEDED)
        if self.per_sample_limit is not None and \
                self._spent_sample.get(sample_key, 0.0) >= self.per_sample_limit:
            raise LLMError("已达单样本费用上限", code=StableErrorCode.LLM_BUDGET_EXCEEDED)

    def record(self, cost: float, sample_key: str = "default") -> None:
        self._spent_today += cost
        self._calls_today += 1
        self._spent_sample[sample_key] = self._spent_sample.get(sample_key, 0.0) + cost


DEFAULT_BUDGET = Budget()


def _estimate_cost(usage: dict, price_version: str = "unpriced") -> float:
    """按 usage 记录估算费用；价格版本未配置时返回 0 并标注 unpriced。"""
    if price_version == "unpriced":
        return 0.0
    return 0.0


def assess_component(*, bundle: EvidenceBundle, provider_id: str,
                     purpose: str = "candidate_review",
                     idempotency_key: str | None = None,
                     budget: Budget | None = None,
                     generation: dict | None = None,
                     force_new: bool = False) -> dict:
    """
    请求一次 LLM 辅助评议。返回持久化的评议记录视图。

    流程：构建请求 → 缓存查找 → 调用（含重试与一次 Schema 修复）→ 后校验 → 落库。
    """
    budget = budget or DEFAULT_BUDGET
    generation = generation or {"temperature": 0}
    prompt_version, prompt_hash = prompt_hashes()

    cfg = repo.LLMProviderRepo.get(provider_id)
    if not cfg.enabled:
        raise LLMError(f"服务商 {provider_id} 未启用", code=StableErrorCode.LLM_PROVIDER_ERROR)

    bundle_dict = bundle.model_dump(mode="json")
    # 哈希只覆盖证据内容本身：bundle_id 是每次新建的随机 ID，
    # 若计入哈希则缓存永远不命中（B11/B16 要求同内容复用同一评议）。
    hashable = {k: v for k, v in bundle_dict.items() if k != "bundle_id"}
    bundle_hash = hashlib.sha256(
        json.dumps(hashable, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    # 幂等键写入生成参数，便于按逻辑请求去重（避免供应商重复计费）
    if idempotency_key:
        generation = {**generation, "idempotency_key": idempotency_key}

    rh = request_hash(bundle_hash=bundle_hash, provider_id=provider_id,
                      config_version=cfg.config_version, prompt_hash=prompt_hash,
                      model=cfg.model, schema_version=LLM_SCHEMA_VERSION, generation=generation)

    # ---- 幂等 / 缓存复用（B11、B16：历史报告读取已存响应，不重新生成）
    if not force_new:
        if idempotency_key:
            existing = _find_by_idempotency(
                idempotency_key, analysis_id=bundle.analysis_id, provider_id=provider_id)
            if existing:
                return existing
        cached = repo.AssessmentRepo.find_by_request_hash(rh)
        if cached:
            return _view(cached, cached_reused=True)

    budget.check(bundle.analysis_id)

    adapter = build_adapter(
        cfg.adapter, base_url=cfg.base_url or "", model=cfg.model or "",
        secret_ref=cfg.secret_ref, timeout=LLM_RUNTIME_DEFAULTS["request_timeout_seconds"],
        **(cfg.config or {}))

    deadline = time.time() + LLM_RUNTIME_DEFAULTS["sample_total_budget_seconds"]
    attempt, result = 0, None
    last_response = None
    last_error: str | None = None
    while attempt <= LLM_RUNTIME_DEFAULTS["max_transport_retries"]:
        attempt += 1
        if time.time() > deadline:
            last_error = "总时限耗尽"
            break
        try:
            res = adapter.assess(bundle_dict, OUTPUT_SCHEMA, generation)
        except LLMError as exc:
            last_error = exc.message
            if attempt <= LLM_RUNTIME_DEFAULTS["max_transport_retries"]:
                time.sleep(min(2 ** attempt, 5))
                continue
            break
        last_response = res
        if res.finish_status == "stop" and res.parsed is not None:
            result = res
            break
        if res.error_code in ("AUTH_ERROR", "SCHEMA_INVALID", "REFUSAL"):
            # 401/403 不重试；拒绝/不可解析最多做一次受限修复
            if res.error_code == "SCHEMA_INVALID" and \
                    LLM_RUNTIME_DEFAULTS["max_schema_repair_attempts"] > 0 and attempt == 1:
                # 受限修复：明确要求只输出 JSON，不提供新的化学信息
                repair = dict(generation)
                repair["repair_instruction"] = "上一次输出不是合法 JSON，请只输出约定 JSON。"
                res2 = adapter.assess(bundle_dict, OUTPUT_SCHEMA, repair)
                last_response = res2
                if res2.finish_status == "stop" and res2.parsed is not None:
                    result = res2
                else:
                    last_error = res2.error_message or "Schema 修复失败"
                break
            last_error = res.error_message
            break
        last_error = res.error_message
        if res.error_code not in ("RATE_LIMITED", "SERVER_ERROR"):
            break
        time.sleep(min(2 ** attempt, 5))

    if result is None:
        # 回退：保留规则结果，记录失败原因（B08/B09）
        aid = new_id("assess")
        repo.AssessmentRepo.save(
            aid, bundle_id=bundle.bundle_id, provider_id=provider_id,
            provider_config_version=cfg.config_version, prompt_version=prompt_version,
            prompt_hash=prompt_hash, request_hash=rh, schema_version=LLM_SCHEMA_VERSION,
            generation_parameters=generation,
            raw_response=last_response.raw_response if last_response else None,
            parsed_response=last_response.parsed if last_response else None,
            finish_status=last_response.finish_status if last_response else "error",
            usage=last_response.usage if last_response else {},
            validation={"status": "failed", "error": last_error or "未知错误",
                        "fallback_to_rule_results": True},
            status="failed")
        return {"assessment_id": aid, "status": "failed",
                "error": last_error or "未知错误",
                "error_code": StableErrorCode.LLM_PROVIDER_ERROR.value,
                "fallback_to_rule_results": True,
                "message": "模型调用失败：保留规则结果，未丢失任何实验结论"}

    # ---- 后校验
    outcome = validate_assessment(result.parsed, bundle_dict,
                                  finish_status=result.finish_status,
                                  raw_response=result.raw_response)
    cost = _estimate_cost(result.usage, (cfg.config or {}).get("price_version", "unpriced"))
    budget.record(cost, bundle.analysis_id)

    aid = new_id("assess")
    repo.AssessmentRepo.save(
        aid, bundle_id=bundle.bundle_id, provider_id=provider_id,
        provider_config_version=cfg.config_version, prompt_version=prompt_version,
        prompt_hash=prompt_hash, request_hash=rh, schema_version=LLM_SCHEMA_VERSION,
        generation_parameters=generation, raw_response=result.raw_response,
        parsed_response=result.parsed, provider_response_model=result.provider_response_model,
        provider_request_id=result.provider_request_id, finish_status=result.finish_status,
        usage=result.usage,
        validation={"status": outcome.status,
                    "hard_failures": outcome.hard_failures,
                    "semantic_flags": outcome.semantic_flags,
                    "pv_checked": outcome.pv_checked},
        status=outcome.status)
    view = _view(repo.AssessmentRepo.get(aid), cached_reused=False)
    view["budget_used"] = cost
    return view


def _view(row, *, cached_reused: bool) -> dict:
    return {
        "assessment_id": row.id,
        "bundle_id": row.bundle_id,
        "status": row.status,
        "finish_status": row.finish_status,
        "provider_id": row.provider_id,
        "provider_config_version": row.provider_config_version,
        "prompt_version": row.prompt_version,
        "schema_version": row.schema_version,
        "usage": row.usage,
        "parsed_response": row.parsed_response,
        "validation": row.validation,
        "reviewer_decision": row.reviewer_decision,
        "cached_reused": cached_reused,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _find_by_idempotency(key: str, *, analysis_id: str, provider_id: str) -> dict | None:
    """幂等键只在同一分析和服务商内复用，避免跨分析返回旧评议。"""
    from ..data.database import session_scope, LLMAssessmentRow, EvidenceBundleRow
    with session_scope() as s:
        from sqlalchemy import select
        row = s.scalar(
            select(LLMAssessmentRow)
            .join(EvidenceBundleRow, LLMAssessmentRow.bundle_id == EvidenceBundleRow.id)
            .where(
                LLMAssessmentRow.generation_parameters["idempotency_key"].as_string() == key,
                LLMAssessmentRow.provider_id == provider_id,
                EvidenceBundleRow.analysis_id == analysis_id,
            ))
        return _view(row, cached_reused=True) if row else None


def record_review(assessment_id: str, *, decision: str, reviewer: str, reason: str) -> dict:
    """人工审核：接受 / 部分接受 / 拒绝。接受建议不等于满足确认门（13.8）。"""
    row = repo.AssessmentRepo.get(assessment_id)
    if decision not in ("accepted", "partially_accepted", "rejected"):
        raise LLMError("审核决定必须是 accepted / partially_accepted / rejected")
    repo.AssessmentRepo.save(assessment_id, reviewer_decision=decision)
    repo.audit("llm_assessment", assessment_id, f"review:{decision}",
               {"reason": reason, "reviewer": reviewer}, reviewer)
    out = _view(repo.AssessmentRepo.get(assessment_id), cached_reused=False)
    out["note"] = ("接受模型建议不会自动提高证据等级；后端需重新计算证据门后方可升级" )
    return out
