"""
样本级 QC 与报告门验收（A17–A19；计划 14.10）。
"""
from __future__ import annotations

from pmaa.analysis.qc import composition_consistency, evaluate_batch_qc, terminal_branch_balance
from pmaa.domain.enums import QCStatus, StableErrorCode
from pmaa.domain.errors import ReportBlockedError
from pmaa.reporting import report as reporting

from _harness import case, eq, ok


@case("A17", "组成未检出只作软约束，不删候选")
def _a17():
    status, msg = composition_consistency(
        ei_sugars={"Gal"}, composition_detected=set(), composition_detection_limit_known=False)
    eq(status, "warning", "应为警告")
    ok(StableErrorCode.COMPOSITION_CONFLICT.value in msg, "应给出 COMPOSITION_CONFLICT")
    ok("保留候选" in msg, "不得删除候选")


@case("A18", "QC 失败：保留候选，冻结正式发布，仅可导出带标识草稿")
def _a18():
    r = evaluate_batch_qc(blank_peak_ratio=0.30, repeat_cv=0.40)
    eq(r.status, QCStatus.failed, "应判失败")
    eq(r.formal_report_allowed, False, "正式报告必须冻结")
    eq(r.blockers, [StableErrorCode.QC_FAILED.value], "阻断码")

    # 草稿导出需要显式允许
    class FakeAnalysis:
        id = "a1"; sample_id = None; method_id = "m1"; library_version = "l1"
        rule_version = "r1"; input_hash = "h" * 64; revision = 1; qc_status = "failed"
    from pmaa.data import repositories as repo
    repo.AnalysisRepo.create("a1", sample_id=None, method_id="m1", library_version="l1",
                             rule_version="r1", input_hash="h" * 64, qc_status="failed")
    payload = reporting.build_report("a1", allow_draft_on_failure=True)
    eq(payload["kind"], "draft_labelled", "仅可导出带标识草稿")
    ok("不得作为签发结果" in payload["draft_warning"], "草稿必须带警告")
    try:
        reporting.build_report("a1", allow_draft_on_failure=False)
    except ReportBlockedError as e:
        eq(e.code, StableErrorCode.QC_FAILED, "阻断错误码")
    else:
        raise AssertionError("QC 失败仍生成了正式报告")


@case("A19", "补充甲基化改变支化峰 → 不得自动给支化结论")
def _a19():
    r = evaluate_batch_qc(remethylation_shift_exceeds_repeatability=True)
    chk = [c for c in r.checks if c.code == "METHYLATION_COMPLETENESS"][0]
    eq(chk.status, "warning", "应为警告")
    ok("不得直接给支化结论" in chk.message, "必须禁止自动支化结论")


@case("A19b", "端基—支化平衡只作调查信号")
def _a19b():
    b = terminal_branch_balance({0: 10, 2: 8, 3: 1}, chains=1)
    eq(b["expected_terminal"], 1 + (1 * 8) + (2 * 1), "期望端基数")
    ok("调查信号" in b["interpretation"], "必须声明只是调查信号")


@case("A19c", "QC 通过时允许正式报告")
def _a19c():
    r = evaluate_batch_qc(blank_peak_ratio=0.01, independent_preparations=3,
                          repeat_cv=0.08, recovery=0.95)
    eq(r.status, QCStatus.passed, "应为通过")
    eq(r.formal_report_allowed, True, "允许正式报告")
