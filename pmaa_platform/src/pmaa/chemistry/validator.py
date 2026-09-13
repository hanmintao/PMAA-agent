"""
化学位点相容性校验（导入期与运行期共用）。

所有规则都是硬规则：不通过 → 隔离（quarantine），不静默修正。
"""
from __future__ import annotations

from ..domain.enums import StableErrorCode
from ..domain.errors import ChemistryRuleError
from .frameworks import Framework, skeleton_of


def validate_positions(*, framework: Framework, o_methyl: list[int],
                       o_acetyl: list[int] | None,
                       n_positions: frozenset[int] = frozenset()) -> list[dict]:
    """
    返回问题列表（空列表 = 通过）。每个问题含稳定错误码，供隔离队列使用。
    """
    problems: list[dict] = []
    available = framework.available_after_n_substitution(n_positions)

    # 1) O-Me 必须在可用集合内（A06：Glcp 不能有 C5 O-Me；A07：Fucp/Rhap 不能有 C6 O-Me）
    bad_me = sorted(set(o_methyl) - set(available))
    if bad_me:
        problems.append({
            "code": StableErrorCode.METHYL_POSITION_INCOMPATIBLE_WITH_FRAMEWORK.value,
            "message": f"O-甲基位置 {bad_me} 不在骨架 {framework.id} 的可用位 {list(available)} 内",
        })

    # 2) N 取代位不得同时列为 O-甲基位（氨基糖 C2 不是普通 O-Me 位）
    clash = sorted(set(o_methyl) & set(n_positions))
    if clash:
        problems.append({
            "code": StableErrorCode.N_METHYL_POSITION_CONFLICT.value,
            "message": f"碳位 {clash} 同时被标为 O-甲基与 N-取代，氨基糖 C2 不能按普通 O-Me 处理",
        })

    # 3) O-Me 与 O-Ac 不得重叠（03 契约语义检查）
    if o_acetyl:
        overlap = sorted(set(o_methyl) & set(o_acetyl))
        if overlap:
            problems.append({
                "code": StableErrorCode.METHYL_POSITION_INCOMPATIBLE_WITH_FRAMEWORK.value,
                "message": f"O-甲基与 O-乙酰位置重叠: {overlap}",
            })

    # 4) 乙酰位必须包含异头碳与环氧碳（水解开环后二者均被乙酰化）
    if o_acetyl:
        must = {framework.anomeric_position, framework.ring_closure_position}
        missing = sorted(must - set(o_acetyl))
        if missing:
            problems.append({
                "code": StableErrorCode.METHYL_POSITION_INCOMPATIBLE_WITH_FRAMEWORK.value,
                "message": f"O-乙酰位缺少异头碳/环氧碳 {missing}（应为 {sorted(must)}）",
            })
    return problems


def validate_sugar_skeleton(parent_sugar: str | None, skeleton: str | None) -> list[dict]:
    """糖名与骨架类别必须自洽；未知糖名不猜，交由调用方决定（可以入库但不可确认）。"""
    if not parent_sugar or not skeleton:
        return []
    expected = skeleton_of(parent_sugar)
    if expected is None:
        return [{
            "code": StableErrorCode.METHYL_POSITION_INCOMPATIBLE_WITH_FRAMEWORK.value,
            "message": f"未知糖名 {parent_sugar!r}，无法核对骨架，按未审条目处理",
            "level": "warning",
        }]
    if expected != skeleton:
        return [{
            "code": StableErrorCode.METHYL_POSITION_INCOMPATIBLE_WITH_FRAMEWORK.value,
            "message": f"糖名 {parent_sugar} 的骨架应为 {expected}，与给定 {skeleton} 不一致",
        }]
    return []


def amino_sugar_branch_note(n_positions: frozenset[int]) -> str | None:
    """
    氨基糖分支：C2 为 N 取代时，中性糖 U 集合不适用；
    该分支尚未完成专项实测验证，因此只能作为候选，不能签发确认（14.4.2 / A08）。
    """
    if not n_positions:
        return None
    return (f"碳位 {sorted(n_positions)} 为 N-取代（氨基糖分支）。已按 N 位剔除后的 U 集合计算映射，"
            f"但该分支未完成专项实测验证，仅可作候选，不可签发自动确认结果。")
