"""统一异常类型。数据层/规则层只抛这些，API 层统一翻译成 HTTP + 稳定错误码。"""
from __future__ import annotations

from typing import Any

from .enums import StableErrorCode


class PlatformError(Exception):
    """所有业务异常基类。"""

    code: StableErrorCode = StableErrorCode.REPORT_BLOCKED
    http_status: int = 400

    def __init__(self, message: str, *, details: Any = None, code: StableErrorCode | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}
        if code is not None:
            self.code = code

    def to_dict(self) -> dict:
        return {"error_code": self.code.value, "message": self.message, "details": self.details}


class ChemistryRuleError(PlatformError):
    code = StableErrorCode.METHYL_POSITION_INCOMPATIBLE_WITH_FRAMEWORK


class ValidationError(PlatformError):
    code = StableErrorCode.METHYL_POSITION_INCOMPATIBLE_WITH_FRAMEWORK
    http_status = 422


class NotFoundError(PlatformError):
    http_status = 404


class IngestError(PlatformError):
    code = StableErrorCode.IMPORT_ROW_ISOLATED
    http_status = 422


class LLMError(PlatformError):
    code = StableErrorCode.LLM_PROVIDER_ERROR
    http_status = 502


class LLMValidationError(PlatformError):
    code = StableErrorCode.LLM_UNSUPPORTED_ASSERTION
    http_status = 422


class ReportBlockedError(PlatformError):
    code = StableErrorCode.REPORT_BLOCKED
    http_status = 409
