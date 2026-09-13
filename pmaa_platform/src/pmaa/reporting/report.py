"""
报告与归档（M10）。

最小字段严格按开发计划第 11 节；QC 失败时只能导出带失败标识的研究草稿（A18）。
报告内容与输入哈希、库/规则/方法版本一起冻结，重跑生成新版本而不改历史。
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import html
import json

from ..config import RULE_VERSION
from ..data import repositories as repo
from ..data.database import new_id
from ..domain.enums import QCStatus
from ..domain.errors import ReportBlockedError
from ..domain.enums import StableErrorCode

MINIMAL_FIELDS = [
    "样本/批次", "实验SOP版本", "GC方法", "NaBD4/NaBH4及标记阶段", "参考库与规则版本",
    "TIC/峰谱", "峰编号/RT", "PMAA", "连接候选", "支持与冲突证据",
    "定量模式/分母", "未识别峰", "QC", "审核者",
]


def build_report(analysis_id: str, *, allow_draft_on_failure: bool = True) -> dict:
    analysis = repo.AnalysisRepo.get(analysis_id)
    components = repo.ComponentRepo.by_analysis(analysis_id)
    sample = None
    try:
        sample = repo.SampleRepo.get(analysis.sample_id) if analysis.sample_id else None
    except Exception:
        sample = None
    method = None
    try:
        method = repo.MethodRepo.get(analysis.method_id) if analysis.method_id else None
    except Exception:
        method = None

    qc = QCStatus(analysis.qc_status)
    formal = qc != QCStatus.failed
    if not formal and not allow_draft_on_failure:
        raise ReportBlockedError(
            "QC 失败：禁止签发正式报告；仅可导出带失败标识的研究草稿",
            code=StableErrorCode.QC_FAILED)

    comp_views = []
    for c in components:
        r = c.result or {}
        decisions = repo.DecisionRepo.by_component(c.id)
        comp_views.append({
            "component_id": c.component_id,
            "peak_label": c.peak_label,
            "rt": c.rt,
            "area": c.area,
            "evidence_type": c.evidence_type,
            "ions": c.ions,
            "derivative_identity_level": r.get("derivative_identity_level"),
            "residue_mapping_level": r.get("residue_mapping_level"),
            "quantification_level": r.get("quantification_level"),
            "candidate_derivatives": r.get("candidate_derivatives", []),
            "candidate_residues": r.get("candidate_residues", []),
            "positive_evidence": r.get("positive_evidence", []),
            "conflicts": r.get("conflicts", []),
            "missing_evidence": r.get("missing_evidence", []),
            "ambiguities": r.get("ambiguities", []),
            "quantification": r.get("quantification", {}),
            "notes": r.get("notes", []),
            "interpretation": r.get("interpretation"),
            "review_history": [{
                "decision_id": d.id, "decision": d.decision, "reviewer": d.reviewer,
                "reason": d.reason, "supersedes_id": d.supersedes_id,
                "created_at": d.created_at.isoformat() if d.created_at else None,
            } for d in decisions],
        })

    preparations = repo.PreparationRepo.by_sample(sample.id) if sample else []
    payload = {
        "report_id": None,
        "analysis_id": analysis_id,
        "analysis_revision": analysis.revision,
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "kind": "formal" if formal else "draft_labelled",
        "sample": ({
            "sample_id": sample.id, "code": sample.code, "source": sample.source,
            "batch": sample.batch, "branch": sample.branch,
            "composition": sample.composition, "modifications": sample.modifications,
        } if sample else None),
        "preparations": [{
            "preparation_batch_id": p.id, "batch_code": p.batch_code,
            "independent_preparation_count": p.independent_preparation_count,
            "post_methylation_aliquots": p.post_methylation_aliquots,
            "steps": p.steps,
        } for p in preparations],
        "method_version": analysis.method_id,
        "method": ({
            "method_id": method.id, "version": method.version, "column": method.column,
            "temperature_program": method.temperature_program,
            "carrier_gas": method.carrier_gas, "ei_energy_ev": method.ei_energy_ev,
            "scan_range_mz": method.scan_range_mz, "reduction_route": method.reduction_route,
            "payload": method.payload,
        } if method else None),
        "library_version": analysis.library_version,
        "rule_version": analysis.rule_version,
        "input_hash": analysis.input_hash,
        "qc_status": analysis.qc_status,
        "qc": (analysis.payload or {}).get("qc"),
        "components": comp_views,
        "minimal_fields": MINIMAL_FIELDS,
        "limitations": _limitations(analysis, comp_views),
        "llm": _llm_section(analysis_id),
        "audit_trail_present": True,
    }
    if not formal:
        payload["draft_warning"] = (
            "本研究草稿由 QC 失败批次生成：正式结构与定量结论已冻结，"
            "以下内容仅供研究，不得作为签发结果。")

    body = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    payload["report_id"] = new_id("report")
    payload["sha256"] = hashlib.sha256(body).hexdigest()
    return payload


def _limitations(analysis, comp_views) -> list[str]:
    notes = []
    if any(c.get("evidence_type") == "qualitative_ion_list" for c in comp_views):
        notes.append("定性离子表输入：未评价色谱纯度及共洗脱；未作摩尔定量。")
    if any(c.get("quantification_level") in ("area_only", "unavailable") for c in comp_views):
        notes.append("未使用响应校准：定量结果为面积占比或不可用，不是准确 mol%。")
    if any(c.get("residue_mapping_level") == "ambiguous" for c in comp_views):
        notes.append("存在同一 PMAA 的多个原残基来源；环型/连接来源待定，需 NMR 等独立证据。")
    if analysis.qc_status in ("warning", "not_assessed"):
        notes.append(f"样本级 QC 状态为 {analysis.qc_status}：结论需结合 QC 明细人工判断。")
    notes.append("α/β 与 D/L 构型不由常规甲基化分析产生；除非另有外部证据，报告中不填写。")
    return notes


def _llm_section(analysis_id: str) -> dict:
    rows = repo.AssessmentRepo.list_for_analysis(analysis_id)
    if not rows:
        return {"enabled": False, "message": "本次分析未进行模型辅助复核。"}
    return {
        "enabled": True,
        "assessments": [{
            "assessment_id": r.id,
            "provider_id": r.provider_id,
            "provider_config_version": r.provider_config_version,
            "model": r.provider_response_model,
            "prompt_version": r.prompt_version,
            "schema_version": r.schema_version,
            "finish_status": r.finish_status,
            "status": r.status,
            "validation": r.validation,
            "reviewer_decision": r.reviewer_decision,
            "usage": r.usage,
        } for r in rows],
        "note": "模型输出为辅助建议；正式结论以经校验的结构化结果为准。",
    }


def save_report(payload: dict) -> dict:
    rid = payload["report_id"]
    repo.ReportRepo.save(rid, analysis_id=payload["analysis_id"],
                         analysis_revision=payload["analysis_revision"],
                         kind=payload["kind"], payload=payload, sha256=payload["sha256"])
    if payload.get("method_version"):
        repo.MethodRepo.lock(payload["method_version"])
    repo.audit("report", rid, "export", {"kind": payload["kind"]}, "system")
    return payload


def render_html(payload: dict) -> str:
    """最小可用的 HTML 报告；数字与状态均从结构化结果渲染，不做二次加工。"""
    rows = []
    for c in payload["components"]:
        derivs = "；".join(html.escape(str(d.get("derivative_name", "")))
                          for d in c.get("candidate_derivatives", [])) or "—"
        residues = "；".join(html.escape(str(r.get("residue_label", "")))
                            for r in c.get("candidate_residues", [])) or "—"
        rows.append(f"""<tr>
	<td>{html.escape(str(c.get('peak_label') or c.get('component_id')))}</td>
<td>{c.get('rt') if c.get('rt') is not None else '—'}</td>
<td>{derivs}</td>
<td>{residues}</td>
<td>{c.get('derivative_identity_level')} / {c.get('residue_mapping_level')}</td>
<td>{c.get('quantification_level')}</td>
<td>{len(c.get('conflicts', []))}</td>
</tr>""")

    lim = "".join(f"<li>{html.escape(str(x))}</li>" for x in payload.get("limitations", []))
    warn = (f"<p class='draft'>{html.escape(str(payload['draft_warning']))}</p>"
            if payload.get("draft_warning") else "")

    interp_blocks = []
    for c in payload["components"]:
        it = c.get("interpretation")
        if not it:
            continue
        segs = "".join(
            f"<li><b>[{i}] {html.escape(str(s.get('tier')))}</b>"
            f"{'（证据门控）' if s.get('gated') else ''}：{html.escape(str(s.get('text')))}</li>"
            for i, s in enumerate((it.get("pmaa_identity"), it.get("residue_linkage"), it.get("sequence")), 1))
        interp_blocks.append(
            f"<p><b>{html.escape(str(c.get('peak_label') or c.get('component_id')))}</b>"
            f"（整体置信度 {html.escape(str(it.get('confidence_tier')))})：</p>"
            f"<ol>{segs}</ol>")
    interp_html = ("<h2>三段式判读（修改方案 P7）</h2>" + "".join(interp_blocks)) if interp_blocks else ""

    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>PMAA 分析报告 {payload['analysis_id']}</title>
<style>
body{{font-family:-apple-system,"PingFang SC",sans-serif;margin:32px;color:#1c2024;background:#fff}}
h1{{font-size:20px}} h2{{font-size:16px;margin-top:24px}}
table{{border-collapse:collapse;width:100%;font-size:13px}}
th,td{{border:1px solid #d8dee6;padding:6px 8px;text-align:left}}
th{{background:#f3f6f9}} .meta{{font-size:13px;color:#4a5568}}
.draft{{background:#fff4e5;border:1px solid #f0b357;padding:10px;border-radius:6px}}
</style></head><body>
<h1>PMAA 甲基化分析报告</h1>
{warn}
<p class="meta">
分析 {payload['analysis_id']} · 修订 {payload['analysis_revision']} ·
方法 {payload['method_version']} · 库 {payload['library_version']} · 规则 {payload['rule_version']}<br/>
输入哈希 {payload['input_hash'][:32]}… · QC <b>{payload['qc_status']}</b> ·
类型 <b>{payload['kind']}</b> · 报告哈希 {payload['sha256'][:16]}…
</p>
<h2>组分与候选</h2>
<table><thead><tr><th>峰</th><th>RT</th><th>PMAA 候选</th><th>连接候选</th>
<th>身份/映射等级</th><th>定量等级</th><th>冲突数</th></tr></thead>
<tbody>{''.join(rows) or '<tr><td colspan="7">无组分</td></tr>'}</tbody></table>
{interp_html}
<h2>局限与未决项</h2><ul>{lim}</ul>
<h2>LLM 辅助复核</h2><pre>{html.escape(json.dumps(payload.get('llm', {}), ensure_ascii=False, indent=1)[:2000])}</pre>
</body></html>"""
