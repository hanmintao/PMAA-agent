"""
PMAA → 原残基连接位点的确定性化学映射引擎（M04）。

核心公式（14.6.1，仅适用于未修饰醛糖且充分甲基化）：
    L = U \\ M          原受体连接位置
    A = {a, r} ∪ L      PMAA 中的 O-乙酰位置

关键实现约束：
* 一条 PMAA 化学实体可对应多个原始残基 → map_derivative() 返回的是**列表**，不是单值；
* 环氧位置 r 在水解开环后也被乙酰化，**不能**误判为连接位点；
* α/β 与 D/L 永不由此产生（返回 None）；
* 全取代（M == U）优先触发欠甲基化/修饰复核，不直接输出罕见全支化结论。
"""
from __future__ import annotations

from dataclasses import dataclass

from ..domain.enums import StableErrorCode
from ..domain.errors import ChemistryRuleError
from ..domain.models import ResidueMapping
from .frameworks import FRAMEWORKS, Framework, frameworks_for_skeleton, get_framework

REQUIRES_ARTIFACT_REVIEW_MSG = (
    "全取代模式（U 中无游离位）优先触发欠甲基化/天然修饰/污染复核，"
    "不直接给出罕见的全位点支化结论（14.6.2 末行）。"
)


@dataclass(frozen=True)
class MappingOutcome:
    framework: Framework
    linkage_positions: tuple[int, ...]
    o_acetyl_positions: tuple[int, ...]
    requires_artifact_review: bool


def compute_mapping(framework_id: str, o_methyl: list[int],
                    n_positions: frozenset[int] = frozenset()) -> MappingOutcome:
    """
    在给定骨架下由 O-甲基位置反推连接位与乙酰位。

    :raises ChemistryRuleError: 甲基位置不在该骨架可用集合内（A06/A07）
    """
    fw = get_framework(framework_id)
    available = fw.available_after_n_substitution(n_positions)
    mset = set(o_methyl)

    bad = sorted(mset - set(available))
    if bad:
        raise ChemistryRuleError(
            f"甲基位置 {bad} 与骨架 {framework_id} 不相容"
            f"（可用位 {list(available)}）",
            details={"framework": framework_id, "invalid_positions": bad,
                     "available": list(available)},
            code=StableErrorCode.METHYL_POSITION_INCOMPATIBLE_WITH_FRAMEWORK,
        )

    linkage = tuple(sorted(set(available) - mset))
    acetyl = tuple(sorted({fw.anomeric_position, fw.ring_closure_position} | set(linkage)))
    return MappingOutcome(
        framework=fw,
        linkage_positions=linkage,
        o_acetyl_positions=acetyl,
        requires_artifact_review=(len(linkage) == len(available) and len(available) > 0),
    )


def map_derivative_by_methyl(o_methyl: list[int], skeleton: str | None = None,
                             n_positions: frozenset[int] = frozenset(),
                             parent_sugar: str | None = None) -> list[ResidueMapping]:
    """
    对所有相容骨架分别生成候选——环型未知时必须全部保留（A04/A05）。

    返回的每个映射都是**候选**，最终 residue_mapping_level 由调用方根据
    候选数量与是否有独立环型证据决定。
    """
    out: list[ResidueMapping] = []
    for fw in frameworks_for_skeleton(skeleton):
        try:
            oc = compute_mapping(fw.id, o_methyl, n_positions)
        except ChemistryRuleError:
            continue  # 该骨架与甲基模式不相容，不是错误，只是不适用
        out.append(ResidueMapping(
            framework_id=fw.id,
            parent_sugar=parent_sugar,
            ring_form=fw.ring_form,
            linkage_positions=list(oc.linkage_positions),
            precondition=(
                "充分甲基化；无未建模 O/N 修饰；骨架与标记路线在支持范围内"
                + ("；全取代模式需人工复核" if oc.requires_artifact_review else "")
            ),
        ))
    return out


def map_from_derivative(skeleton: str, o_methyl: list[int], o_acetyl: list[int],
                        n_positions: frozenset[int] = frozenset()) -> list[MappingOutcome]:
    """
    由 PMAA 实体的 (骨架, O-Me, O-Ac) 反解所有能生成它的原始来源。
    这是"同一衍生物多个来源"歧义（identical_derivative_origins）的检测入口。
    """
    results: list[MappingOutcome] = []
    for fw in frameworks_for_skeleton(skeleton):
        available = fw.available_after_n_substitution(n_positions)
        # 反解：linkage = acetyl \ {a, r}，且必须与 U 自洽
        linkage = tuple(sorted(set(o_acetyl) - {fw.anomeric_position, fw.ring_closure_position}))
        if not set(linkage) <= set(available):
            continue
        expected_methyl = tuple(sorted(set(available) - set(linkage)))
        if expected_methyl != tuple(sorted(set(o_methyl))):
            continue
        results.append(MappingOutcome(
            framework=fw, linkage_positions=linkage,
            o_acetyl_positions=tuple(sorted(o_acetyl)),
            requires_artifact_review=(len(linkage) == len(available) and len(available) > 0),
        ))
    return results


def canonical_derivative_key(skeleton: str, o_methyl: list[int], o_acetyl: list[int],
                             label_key: str = "NaBD4_CH3I") -> str:
    """化学实体的规范键。刻意不含"原残基名称"——同一实体可有多个来源。"""
    return (f"{skeleton}|Me{'.'.join(str(x) for x in sorted(o_methyl)) or '-'}"
            f"|Ac{'.'.join(str(x) for x in sorted(o_acetyl))}|{label_key}")


def derivative_display_name(skeleton: str, o_methyl: list[int], o_acetyl: list[int]) -> str:
    me = ",".join(str(x) for x in sorted(o_methyl)) or "-"
    ac = ",".join(str(x) for x in sorted(o_acetyl))
    return f"{skeleton}醇 1-氘-{me}-O-甲基-{ac}-O-乙酰 (PMAA)"


def traditional_linkage_label(linkage: list[int], ring_form: str, sugar: str | None = None) -> str:
    """
    传统含 C1 写法，如 [3,4,6] + p + Gal → "1,3,4,6-Galp"。
    注意这只是**记号**，不等于糖链序列。
    """
    parts = [1] + sorted(linkage)
    label = ",".join(str(p) for p in parts)
    suffix = f"{sugar or ''}{ring_form}" if sugar else ring_form
    return f"{label}-{suffix}"
