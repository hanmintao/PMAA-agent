"""
LLM 服务商适配器（M11 / 13.2）。

统一接口：
    capabilities() -> dict
    assess(evidence_bundle, output_schema, generation_config) -> ProviderResult

提供三个适配器：
  * MockAdapter         —— 离线确定性响应，用于测试与无密钥部署；
  * OpenAIChatAdapter   —— Chat Completions 兼容端点（含 response_format json_schema）；
  * OpenAIResponsesAdapter —— Responses API（结构化输出）。

密钥只从服务端 secret_ref 对应的环境变量读取，数据库不存明文。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..config import LLM_RUNTIME_DEFAULTS
from ..domain.errors import LLMError
from ..domain.enums import StableErrorCode


@dataclass
class ProviderResult:
    raw_response: str
    parsed: dict | None
    finish_status: str          # stop / length / refusal / content_filter / error
    usage: dict = field(default_factory=dict)
    provider_response_model: str | None = None
    provider_request_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None


class ProviderAdapter(Protocol):
    def capabilities(self) -> dict: ...
    def assess(self, bundle: dict, output_schema: dict,
               generation_config: dict) -> ProviderResult: ...


# ====================================================================== Mock
class MockAdapter:
    """
    离线适配器：不联网、不消耗费用，输出**确定性**的保守评议。

    行为刻意保守：全部候选给 insufficient、保留全部歧义与缺失项。
    这样即使在没有密钥的环境，端到端流程与后校验链路也能完整验证（B15）。
    """

    def __init__(self, model: str = "mock-deterministic-v1", **_):
        self.model = model

    def capabilities(self) -> dict:
        return {"structured_output": True, "tool_calling": False,
                "supports_temperature": False, "supports_seed": False, "offline": True}

    def assess(self, bundle: dict, output_schema: dict, generation_config: dict) -> ProviderResult:
        candidates = [c["candidate_id"] for c in bundle.get("candidates", [])]
        amb_ids = [a["ambiguity_id"] for a in bundle.get("ambiguities", [])]
        missing = list(bundle.get("missing_evidence_codes", []))
        parsed = {
            "schema_version": "1.0",
            "component_id": bundle["component_id"],
            "summary": "离线模拟评议：保留全部候选与歧义，未新增任何确认结论。",
            "ranking_groups": [{"candidate_ids": candidates}] if candidates else [],
            "candidate_reviews": [
                {"candidate_id": cid, "assessment": "insufficient",
                 "evidence_ids": [f["fact_id"] for f in bundle.get("facts", [])][:3],
                 "rule_ids": [r["rule_id"] for r in bundle.get("rules", [])][:2],
                 "reason_summary": "证据不足，需补充原残基环型、完整 EI 与同方法保留证据。"}
                for cid in candidates
            ],
            "retained_ambiguity_ids": amb_ids,
            "missing_evidence_codes": missing,
            "next_actions": [
                {"action_code": "REVIEW_NMR_RING_EVIDENCE", "evidence_ids": [],
                 "purpose": "区分原残基的吡喃型与呋喃型来源。"},
            ] if "RING_FORM_EVIDENCE" in missing else [
                {"action_code": "MANUAL_REVIEW", "evidence_ids": [], "purpose": "交分析负责人人工复核。"}
            ],
            "recommended_disposition": "needs_review",
        }
        return ProviderResult(
            raw_response=json.dumps(parsed, ensure_ascii=False),
            parsed=parsed, finish_status="stop",
            usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            provider_response_model=self.model,
            provider_request_id=f"mock_{uuid.uuid4().hex[:12]}",
        )


# ====================================================================== HTTP 基类
class _HTTPAdapterBase:
    def __init__(self, *, base_url: str, model: str, secret_ref: str | None = None,
                 api_key_env: str | None = None, timeout: float | None = None, **extra):
        self.base_url = (base_url or "").rstrip("/")
        self.model = model
        self.secret_ref = secret_ref
        self.api_key_env = api_key_env or (secret_ref.upper() if secret_ref else None)
        self.timeout = timeout or LLM_RUNTIME_DEFAULTS["request_timeout_seconds"]
        self.extra = extra

    def _api_key(self) -> str | None:
        if not self.api_key_env:
            return None
        return os.environ.get(self.api_key_env)

    def _headers(self) -> dict:
        key = self._api_key()
        h = {"Content-Type": "application/json"}
        if key:
            h["Authorization"] = f"Bearer {key}"
        return h

    def _post(self, url: str, payload: dict) -> tuple[int, dict, str]:
        """返回 (status, json, raw_text)。401/403 不重试，由网关层判断。"""
        import httpx
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(url, headers=self._headers(), json=payload)
                raw = resp.text
                try:
                    body = resp.json()
                except Exception:
                    body = {}
                return resp.status_code, body, raw
        except Exception as exc:  # 网络层异常统一包装，不泄漏底层堆栈给业务层
            raise LLMError(f"网络请求失败: {type(exc).__name__}: {exc}",
                           details={"url": url},
                           code=StableErrorCode.LLM_PROVIDER_ERROR)

    @staticmethod
    def _finish(status_code: int, body: dict, raw_response: str = "") -> ProviderResult:
        if status_code in (401, 403):
            return ProviderResult(raw_response=raw_response, parsed=None, finish_status="error",
                                  error_code="AUTH_ERROR",
                                  error_message=f"认证失败（HTTP {status_code}）：不重复请求，请检查密钥配置")
        if status_code == 429:
            return ProviderResult(raw_response=raw_response, parsed=None, finish_status="error",
                                  error_code="RATE_LIMITED", error_message="供应商限流（429）")
        if status_code >= 500:
            return ProviderResult(raw_response=raw_response, parsed=None, finish_status="error",
                                  error_code="SERVER_ERROR", error_message=f"供应商服务错误（HTTP {status_code}）")
        return ProviderResult(raw_response=raw_response, parsed=None, finish_status="error",
                              error_code="HTTP_ERROR", error_message=f"未预期的 HTTP {status_code}")


class OpenAIChatAdapter(_HTTPAdapterBase):
    """Chat Completions 兼容适配器。兼容 HTTP 路径不等于能力等价，故显式声明能力。"""

    def capabilities(self) -> dict:
        return {"structured_output": "best_effort", "tool_calling": True,
                "supports_temperature": True, "supports_seed": True, "offline": False}

    def assess(self, bundle: dict, output_schema: dict, generation_config: dict) -> ProviderResult:
        from .prompts import SYSTEM_PROMPT, build_user_prompt
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_prompt(
                    json.dumps(bundle, ensure_ascii=False), bundle["component_id"])},
            ],
            "temperature": generation_config.get("temperature", 0),
        }
        if output_schema and self.extra.get("use_json_schema", True):
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "pmaa_assessment", "strict": True, "schema": output_schema},
            }
        status, body, raw = self._post(url, payload)
        if status != 200:
            return self._finish(status, body, raw)

        choice = (body.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        content = message.get("content") or ""
        finish = choice.get("finish_reason") or "stop"
        if message.get("refusal"):
            return ProviderResult(raw_response=raw, parsed=None, finish_status="refusal",
                                  error_code="REFUSAL", error_message=str(message["refusal"]),
                                  usage=body.get("usage", {}),
                                  provider_response_model=body.get("model"),
                                  provider_request_id=body.get("id"))
        try:
            parsed = json.loads(content)
        except Exception:
            return ProviderResult(raw_response=raw, parsed=None, finish_status="unparsable",
                                  error_code="SCHEMA_INVALID", error_message="响应不是合法 JSON")
        return ProviderResult(
            raw_response=raw, parsed=parsed, finish_status=finish,
            usage=body.get("usage", {}), provider_response_model=body.get("model"),
            provider_request_id=body.get("id"),
        )


class OpenAIResponsesAdapter(_HTTPAdapterBase):
    """Responses API 适配器，使用严格 JSON Schema 结构化输出。"""

    def capabilities(self) -> dict:
        return {"structured_output": True, "tool_calling": True,
                "supports_temperature": False, "supports_seed": False, "offline": False}

    def assess(self, bundle: dict, output_schema: dict, generation_config: dict) -> ProviderResult:
        from .prompts import SYSTEM_PROMPT, build_user_prompt
        url = f"{self.base_url}/responses"
        payload = {
            "model": self.model,
            "input": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_prompt(
                    json.dumps(bundle, ensure_ascii=False), bundle["component_id"])},
            ],
        }
        if output_schema:
            payload["text"] = {
                "format": {"type": "json_schema", "name": "pmaa_assessment",
                           "strict": True, "schema": output_schema}
            }
        status, body, raw = self._post(url, payload)
        if status != 200:
            return self._finish(status, body, raw)

        response_status = body.get("status", "completed")
        if response_status == "incomplete":
            reason = (body.get("incomplete_details") or {}).get("reason", "unknown")
            return ProviderResult(
                raw_response=raw, parsed=None, finish_status="length",
                error_code="INCOMPLETE", error_message=f"Responses API 输出不完整: {reason}",
                usage=body.get("usage", {}), provider_response_model=body.get("model"),
                provider_request_id=body.get("id"))

        text = ""
        refusal_text = ""
        for item in body.get("output", []):
            for c in item.get("content", []):
                if c.get("type") == "output_text":
                    text += c.get("text", "")
                elif c.get("type") == "refusal":
                    refusal_text += c.get("refusal", "")
        refusal = refusal_text or body.get("error") or {}
        if not text:
            return ProviderResult(raw_response=raw, parsed=None, finish_status="refusal",
                                  error_code="REFUSAL",
                                  error_message=f"模型拒绝或未产出内容: {refusal}")
        try:
            parsed = json.loads(text)
        except Exception:
            return ProviderResult(raw_response=raw, parsed=None, finish_status="unparsable",
                                  error_code="SCHEMA_INVALID", error_message="响应不是合法 JSON")
        return ProviderResult(
            raw_response=raw, parsed=parsed,
            # Responses API 的成功状态是 completed；网关和后校验统一使用 stop。
            finish_status="stop" if response_status == "completed" else response_status,
            usage=body.get("usage", {}),
            provider_response_model=body.get("model"),
            provider_request_id=body.get("id"),
        )


ADAPTERS: dict[str, type] = {
    "mock": MockAdapter,
    "openai_chat": OpenAIChatAdapter,
    "openai_responses": OpenAIResponsesAdapter,
}


def build_adapter(adapter_type: str, **kw) -> ProviderAdapter:
    if adapter_type not in ADAPTERS:
        raise LLMError(f"未知适配器类型: {adapter_type}",
                       code=StableErrorCode.LLM_PROVIDER_ERROR)
    return ADAPTERS[adapter_type](**kw)
