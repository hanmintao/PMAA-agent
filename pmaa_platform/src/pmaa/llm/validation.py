"""
LLM 输出后校验（M13 / 04 契约 PV01–PV10）。

两条铁律：
  1. 引用 ID 存在只是**必要条件**，不自动证明语义支持（PV06）；
  2. 模型失败/被拒收时，**原算法结果必须保留**（13.5 末）。

校验结果分两类：
  * hard_fail  —— 直接进入 rejected，不入库为有效评议；
  * semantic_flags —— 交人工语义复核，标记后不得进入正式报告。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from ..config import MISSING_EVIDENCE_CODES
from ..domain.enums import StableErrorCode

# 越界断言关键词（出现即打语义标记，需人工确认是否真的越界）
_STEREO_PATTERNS = [
    r"[αβ]-?[DL]?[- ]?(?:Glc|Gal|Man|Ara|Xyl|Fuc|Rha|Hex|Pen)",
    r"[DL]-[A-Z][a-z]{2}[pf](?!\w*候选)",
    r"异头构型",
]
_MOLAR_PATTERNS = [r"\bmol\s*%", r"摩尔百分比", r"摩尔占比", r"准确摩尔"]
_CONFIRM_PATTERNS = [r"\bconfirmed\b", r"已确认", r"确认为", r"可以确认"]
_FULL_SPECTRUM_PATTERNS = [r"完整数值谱", r"全谱确认", r"full spectrum confirm"]
_RT_PATTERN = re.compile(r"(\d+\.?\d*)\s*(?:min|分钟)")
_MZ_PATTERN = re.compile(r"\bm/?z\s*(\d{2,3}(?:\.\d)?)\b")


@dataclass
class ValidationOutcome:
    status: str                                  # accepted / rejected / needs_review
    hard_failures: list[dict] = field(default_factory=list)
    semantic_flags: list[dict] = field(default_factory=list)
    pv_checked: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "accepted"


def _fail(code: str, message: str, rule: str) -> dict:
    return {"code": code, "message": message, "rule": rule}


def validate_assessment(parsed: dict, bundle: dict, *, finish_status: str,
                        raw_response: str) -> ValidationOutcome:
    out = ValidationOutcome(status="accepted")
    pv = out.pv_checked

    # ---------------------------------------------------------------- PV07
    pv.append("PV07")
    if finish_status in ("refusal", "unparsable", "error", "length"):
        out.hard_failures.append(_fail(
            "LLM_RESPONSE_UNUSABLE",
            f"响应不可用（finish_status={finish_status}）：拒绝、截断或不可解析的响应一律拒收",
            "PV07"))
        out.status = "rejected"
        return out
    if parsed is None:
        out.hard_failures.append(_fail("LLM_RESPONSE_UNUSABLE", "无解析结果", "PV07"))
        out.status = "rejected"
        return out

    # ---------------------------------------------------------------- PV01
    pv.append("PV01")
    if parsed.get("component_id") != bundle.get("component_id"):
        out.hard_failures.append(_fail(
            "LLM_COMPONENT_MISMATCH",
            f"component_id 应为 {bundle.get('component_id')}，实际 {parsed.get('component_id')}",
            "PV01"))

    bundle_candidates = {c["candidate_id"] for c in bundle.get("candidates", [])}
    fact_ids = {f["fact_id"] for f in bundle.get("facts", [])}
    rule_ids = {r["rule_id"] for r in bundle.get("rules", [])}
    amb_ids = {a["ambiguity_id"] for a in bundle.get("ambiguities", [])}

    reviews = parsed.get("candidate_reviews", []) or []
    for rv in reviews:
        unknown_ev = [e for e in rv.get("evidence_ids", []) if e not in fact_ids]
        if unknown_ev:
            out.hard_failures.append(_fail(
                StableErrorCode.LLM_UNKNOWN_EVIDENCE.value,
                f"引用了不存在的事实 ID: {unknown_ev}（B01）", "PV01"))
        unknown_ru = [r for r in rv.get("rule_ids", []) if r not in rule_ids]
        if unknown_ru:
            out.hard_failures.append(_fail(
                StableErrorCode.LLM_UNKNOWN_EVIDENCE.value,
                f"引用了不存在的规则 ID: {unknown_ru}", "PV01"))

    # ---------------------------------------------------------------- PV02
    pv.append("PV02")
    groups = parsed.get("ranking_groups", []) or []
    grouped: list[str] = []
    dup = False
    for g in groups:
        for cid in g.get("candidate_ids", []):
            if cid in grouped:
                dup = True
            grouped.append(cid)
    if dup:
        out.hard_failures.append(_fail("LLM_DUPLICATE_RANKING", "ranking_groups 中存在重复候选", "PV02"))
    missing_in_groups = bundle_candidates - set(grouped)
    if missing_in_groups:
        out.hard_failures.append(_fail(
            StableErrorCode.LLM_DROPPED_AMBIGUITY.value,
            f"ranking_groups 未覆盖候选 {sorted(missing_in_groups)}（B02）", "PV02"))
    extra_in_groups = set(grouped) - bundle_candidates
    if extra_in_groups:
        out.hard_failures.append(_fail(
            StableErrorCode.LLM_DROPPED_AMBIGUITY.value,
            f"ranking_groups 引入了证据包外的候选 {sorted(extra_in_groups)}（PV10：新候选须先经独立检索）",
            "PV02/PV10"))
    # 同一 PMAA 的来源必须同组（不可拆成不同优先级组）
    for amb in bundle.get("ambiguities", []):
        if amb.get("type") == "identical_derivative_origins":
            members = [c for c in amb.get("candidate_ids", []) if c in bundle_candidates]
            if not members:
                continue
            owner = [i for i, g in enumerate(groups) if any(m in g.get("candidate_ids", []) for m in members)]
            if len(set(owner)) > 1:
                out.hard_failures.append(_fail(
                    StableErrorCode.LLM_DROPPED_AMBIGUITY.value,
                    f"同一 PMAA 的并列来源 {members} 被拆到了不同排序组（B02）", "PV02"))

    # ---------------------------------------------------------------- PV03
    pv.append("PV03")
    reviewed = [rv.get("candidate_id") for rv in reviews]
    if len(reviewed) != len(set(reviewed)):
        out.hard_failures.append(_fail("LLM_DUPLICATE_REVIEW", "candidate_reviews 存在重复候选", "PV03"))
    if bundle_candidates - set(reviewed):
        out.hard_failures.append(_fail(
            "LLM_INCOMPLETE_REVIEW",
            f"candidate_reviews 未覆盖候选 {sorted(bundle_candidates - set(reviewed))}", "PV03"))

    # ---------------------------------------------------------------- PV04
    pv.append("PV04")
    retained = set(parsed.get("retained_ambiguity_ids", []) or [])
    if amb_ids - retained:
        out.hard_failures.append(_fail(
            StableErrorCode.LLM_DROPPED_AMBIGUITY.value,
            f"未保留必保留歧义 {sorted(amb_ids - retained)}（B02）", "PV04"))
    in_missing = set(bundle.get("missing_evidence_codes", []))
    out_missing = set(parsed.get("missing_evidence_codes", []) or [])
    if in_missing - out_missing:
        out.hard_failures.append(_fail(
            "LLM_DROPPED_MISSING_EVIDENCE",
            f"输入缺失项 {sorted(in_missing - out_missing)} 未被保留（PV04）", "PV04"))
    unknown_codes = out_missing - in_missing - MISSING_EVIDENCE_CODES
    if unknown_codes:
        out.hard_failures.append(_fail(
            "LLM_UNKNOWN_MISSING_CODE",
            f"新增缺失项代码 {sorted(unknown_codes)} 不在已审核代码表中", "PV04"))

    # ---------------------------------------------------------------- PV05 / 语义越界
    pv.append("PV05")
    text_all = json.dumps(parsed, ensure_ascii=False)
    ceiling = bundle.get("evidence_ceiling", {})
    for pat in _STEREO_PATTERNS:
        if re.search(pat, text_all):
            out.semantic_flags.append({
                "code": StableErrorCode.LLM_UNSUPPORTED_ASSERTION.value,
                "message": f"输出中出现立体构型断言（匹配 {pat}）："
                           f"常规甲基化分析不产生 α/β 与 D/L，需人工语义复核（B03）",
                "rule": "PV05",
            })
            break
    if not ceiling.get("calibrated_quantification_available", False):
        for pat in _MOLAR_PATTERNS:
            if re.search(pat, text_all):
                out.semantic_flags.append({
                    "code": StableErrorCode.LLM_UNSUPPORTED_ASSERTION.value,
                    "message": "无校准却在输出中声称摩尔比例：不得创建 calibrated_molar 结果（B06）",
                    "rule": "PV05",
                })
                break
    if bundle.get("evidence_type") == "qualitative_ion_list":
        for pat in _FULL_SPECTRUM_PATTERNS + _CONFIRM_PATTERNS:
            if re.search(pat, text_all):
                out.semantic_flags.append({
                    "code": StableErrorCode.LLM_UNSUPPORTED_ASSERTION.value,
                    "message": "定性离子表输入下声称完整谱确认：不得提升证据等级（B05）",
                    "rule": "PV05",
                })
                break

    # ---------------------------------------------------------------- PV09 数值回写检查
    pv.append("PV09")
    fact_numbers = set()
    for f in bundle.get("facts", []):
        v = f.get("value")
        if isinstance(v, (int, float)):
            fact_numbers.add(round(float(v), 3))
        elif isinstance(v, list):
            fact_numbers.update(round(float(x), 3) for x in v if isinstance(x, (int, float)))
    rt_claims = [float(x) for x in _RT_PATTERN.findall(text_all)]
    for v in rt_claims:
        if round(v, 3) not in fact_numbers:
            out.semantic_flags.append({
                "code": "LLM_FABRICATED_MEASUREMENT",
                "message": f"输出中出现证据包未记载的保留时间 {v} min：不得回写观测记录（B04）",
                "rule": "PV09",
            })
    mz_claims = [float(x) for x in _MZ_PATTERN.findall(text_all)]
    bundle_mz = set()
    for f in bundle.get("facts", []):
        if str(f.get("field", "")).startswith("ion."):
            try:
                bundle_mz.add(float(str(f["field"]).split(".", 1)[1]))
            except Exception:
                pass
    for v in mz_claims:
        if bundle_mz and v not in bundle_mz:
            out.semantic_flags.append({
                "code": "LLM_FABRICATED_MEASUREMENT",
                "message": f"输出中出现证据包未记载的 m/z {v}：不得当作实测峰（B04）",
                "rule": "PV09",
            })

    # ---------------------------------------------------------------- PV08
    pv.append("PV08")
    if bundle.get("qc_status") == "failed":
        out.semantic_flags.append({
            "code": StableErrorCode.LLM_UNSUPPORTED_ASSERTION.value,
            "message": "QC 失败：模型输出只能作为故障分析建议，不得发布正式结构/定量结论（PV08）",
            "rule": "PV08",
        })

    pv.append("PV06")
    if out.hard_failures:
        out.status = "rejected"
    elif out.semantic_flags:
        out.status = "needs_review"
    return out
