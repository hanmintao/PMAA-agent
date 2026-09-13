"""
证据包构建（M12 / 13.4）。

原则：
  * 证据包由**服务端**构建，客户端不能塞入未经验证的确认标签；
  * 一次请求绑定一个固定 analysis_revision；
  * 只发送当前分析所需的脱敏证据；
  * 包含并列等价类与被保留的歧义，不得为"制造唯一解"而截掉同一 PMAA 的另一个来源。
"""
from __future__ import annotations

import hashlib
import json

from ..data import repositories as repo
from ..data.database import new_id
from ..domain.models import (
    Ambiguity,
    EvidenceBundle,
    EvidenceCeiling,
    Fact,
    RuleItem,
)

# 已审核规则（只发送已审核版本；规则文本与计划第 2 节的科学产品规则一致）
RULE_LIBRARY: list[RuleItem] = [
    RuleItem(rule_id="R_MULTI_ORIGIN",
             text="一条 PMAA 化学实体可对应多个原始残基；衍生物身份与原残基映射分别判定。"),
    RuleItem(rule_id="R_RING_ORIGIN",
             text="环型未知时，同一 PMAA（如 2,3,6-Me3 / 1,4,5-Ac3）可来自 4-Hexp 或 5-Hexf，必须同时保留。"),
    RuleItem(rule_id="R_NO_STEREO",
             text="常规非手性 GC–EI 甲基化分析不产生 α/β 与 D/L 结论；外部证据确认后方可显示并标注来源。"),
    RuleItem(rule_id="R_QUALITATIVE_CEILING",
             text="定性离子表不得生成数值强度，不得跑全谱余弦，证据上限为 candidate。"),
    RuleItem(rule_id="R_UNLISTED_ION",
             text="未列出的离子不等于未检出；缺测、未报告与明确未检出是三种不同数据状态。"),
    RuleItem(rule_id="R_RETENTION_METHOD",
             text="RT/RRT/RI 分开保存；方法不匹配时不应用文献固定容差，也不得据此硬排除。"),
    RuleItem(rule_id="R_SCORE_NOT_PROBABILITY",
             text="最高匹配分数不是确认结论，相似度分数不是正确概率。"),
    RuleItem(rule_id="R_QUANT_CALIBRATION",
             text="无校准时只能输出面积占比或明示半定量，不得输出准确 mol%。"),
    RuleItem(rule_id="R_QC_FREEZE",
             text="QC 失败后保留候选供研究，但冻结正式结构与定量放行。"),
    RuleItem(rule_id="R_LLM_BOUNDARY",
             text="LLM 不得生成新的实测峰、保留值、校准值或实验条件执行记录；输出为建议，不是结论。"),
]


def build_bundle(*, analysis_id: str, analysis_revision: int, component_id: str,
                 result: dict, component_row, library_version: str,
                 rule_version: str, method_version: str,
                 evidence_type: str, spectrum_completeness: str,
                 input_hash: str, qc_status: str,
                 experimental_context: dict | None = None) -> EvidenceBundle:
    """由已计算好的 ComponentResult 构造证据包，并计算内容哈希用于缓存/重放。"""
    facts: list[Fact] = []
    n = 0

    def _fact(field: str, value, unit=None, basis="observed", locator="analysis_result"):
        nonlocal n
        n += 1
        facts.append(Fact(fact_id=f"F{n:03d}", field=field, value=value, unit=unit,
                          basis=basis, source_locator=locator))

    if component_row is not None:
        if component_row.rt is not None:
            _fact("component.rt", component_row.rt, "min", "observed", "peak_table")
        if component_row.area is not None:
            _fact("component.area", component_row.area, "area_unit", "observed", "peak_table")
    _fact("component.evidence_type", evidence_type, None, "observed", "peak_table")
    _fact("component.spectrum_completeness", spectrum_completeness, None, "observed", "peak_table")
    _fact("component.input_hash", input_hash, None, "derived", "analysis")

    if experimental_context:
        batch_id = experimental_context.get("preparation_batch_id", "unknown")
        _fact("preparation.batch_id", batch_id, None, "observed", "preparation_batch")
        _fact("preparation.independent_preparation_count",
              experimental_context.get("independent_preparation_count"), None,
              "observed", f"preparation:{batch_id}")
        _fact("preparation.post_methylation_aliquots",
              experimental_context.get("post_methylation_aliquots"), None,
              "observed", f"preparation:{batch_id}")
        for step_code, record in sorted((experimental_context.get("records") or {}).items()):
            # 仅传结构化实验事实；不把未填写步骤补成已执行。
            _fact(f"preparation.{step_code}", record, None, "observed",
                  f"preparation:{batch_id}:{step_code}")

    for ion in (component_row.ions if component_row is not None else []):
        _fact(f"ion.{ion.get('mz')}", ion.get("marker") or ion.get("intensity"), None,
              "observed", "peak_table")

    for cand in result.get("candidate_derivatives", []):
        _fact(f"candidate.{cand['candidate_id']}.o_methyl_positions",
              cand["o_methyl_positions"], None, "derived", "rule_candidate")
        _fact(f"candidate.{cand['candidate_id']}.match_score", cand["score"], None,
              "derived", "search")
        _fact(f"candidate.{cand['candidate_id']}.identity_level",
              cand["identity_level"], None, "derived", "rule_engine")

    for res in result.get("candidate_residues", []):
        _fact(f"residue.{res['residue_label']}.linkage_positions",
              res["linkage_positions"], None, "derived", "mapping_engine")
        _fact(f"residue.{res['residue_label']}.mapping_level",
              res["mapping_level"], None, "derived", "mapping_engine")

    for code in result.get("missing_evidence", []):
        _fact(f"missing_evidence.{code}", True, None, "unknown", "rule_engine")

    candidates_payload = []
    residues_by_derivative: dict[str, list[dict]] = {}
    for residue in result.get("candidate_residues", []):
        did = residue.get("derivative_id")
        if did:
            residues_by_derivative.setdefault(did, []).append(residue)
    for cand in result.get("candidate_derivatives", []):
        matched_residues = residues_by_derivative.get(cand["derivative_id"], [])
        for res in matched_residues:
            candidates_payload.append({
                "candidate_id": f"{cand['candidate_id']}::{res['residue_label']}",
                "derivative_id": cand["derivative_id"],
                "residue_label": res["residue_label"],
                "source_ids": cand.get("supported_by", []),
            })
        if not matched_residues:
            candidates_payload.append({
                "candidate_id": f"{cand['candidate_id']}::unknown",
                "derivative_id": cand["derivative_id"],
                "residue_label": "unknown",
                "source_ids": cand.get("supported_by", []),
            })

    ambiguities = [Ambiguity(**a) if isinstance(a, dict) else a
                   for a in result.get("ambiguities", [])]

    ceiling = EvidenceCeiling(
        maximum_derivative_identity_level=result.get("derivative_identity_level", "candidate"),
        residue_mapping_must_remain_ambiguous=(
            result.get("residue_mapping_level") == "ambiguous"),
        calibrated_quantification_available=(
            result.get("quantification_level") == "calibrated_molar"),
    )

    payload = {
        "schema_version": "1.0",
        "analysis_id": analysis_id,
        "analysis_revision": analysis_revision,
        "component_id": component_id,
        "evidence_type": evidence_type,
        "spectrum_completeness": spectrum_completeness,
        "input_hash": input_hash,
        "method_version": method_version,
        "library_version": library_version,
        "rule_version": rule_version,
        "qc_status": qc_status,
        "facts": [f.model_dump() for f in facts],
        "candidates": candidates_payload,
        "rules": [r.model_dump() for r in RULE_LIBRARY],
        "ambiguities": [a.model_dump() for a in ambiguities],
        "missing_evidence_codes": result.get("missing_evidence", []),
        "evidence_ceiling": ceiling.model_dump(),
    }
    bhash = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    bundle_id = new_id("bundle")
    bundle = EvidenceBundle(bundle_id=bundle_id, **payload)

    # 冻结保存：发给模型的内容必须可回放（13.4 / B16）
    repo.BundleRepo.save(bundle_id, analysis_id=analysis_id,
                         analysis_revision=analysis_revision, component_id=component_id,
                         bundle_hash=bhash, payload=bundle.model_dump(mode="json"))
    return bundle
