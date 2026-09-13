"""
实验性 PMAA 高质量离子簇诊断（不用于生产硬门控）。

早期版本曾尝试从三个高质量碎片猜分子离子，再用骨架类别固定 MW 删除候选。
该假设不适合生产判定：PMAA 的甲基/乙酰取代数不同会改变衍生物分子量，
EI 分子离子也可能很弱或缺失。因此 analysis.pipeline 不再调用本模块；这些函数只保留
给历史回归和方法学研究，调用者不得把返回值视为鉴定证据。

分子量采用还原醛醇乙酸酯（PMAA）在 NaBD4 / NaBH4 下的名义值（单位 m/z，取整）：
  NaBD4: Hex≈260, dHex≈232, Pen≈204
  NaBH4: Hex≈259, dHex≈231, Pen≈203   （1 位少一个 D，约 -1）
下列数值只对应旧验收样例，不代表各骨架所有 PMAA 的通用分子量。
"""
from __future__ import annotations

# 各骨架在两种还原标签下的名义分子量（m/z）。
SKELETON_MW: dict[str, dict[str, int]] = {
    "NaBD4": {"Hex": 260, "dHex": 232, "Pen": 204},
    "NaBH4": {"Hex": 259, "dHex": 231, "Pen": 203},
}

# M−15 = 失去 CH3；M−28 = 失去 CO 或 C2H4；M−42 = 失去 CH2CO 或 C3H6。
# 三者同期出现即可较稳地定位分子离子区。
_LOSS_OFFSETS = (15, 13, 27)  # 对应 M-15, M-28(=M-15-13), M-42(=M-15-27)


def estimate_molecular_weight(ion_mzs: list[float]) -> int | None:
    """
    从观测离子 m/z 估计分子离子 M。

    只在高质量区能找到 ``{M-15, M-28, M-42}`` 三件套（±1 m/z 校准误差）时才返回，
    否则返回 None —— 不强行门控，避免对缺高质量区信息的谱图误伤（尊重 A02）。
    """
    s = {round(float(x)) for x in ion_mzs}
    if not s:
        return None
    for m in sorted(s, reverse=True):          # 从最高 m/z 往下找
        if (m - _LOSS_OFFSETS[1]) in s and (m - _LOSS_OFFSETS[2]) in s:
            return m + _LOSS_OFFSETS[0]        # M = (M-15) + 15
    return None


def allowed_skeletons(m_est: int, reduction_label: str, tol: float) -> list[str]:
    """
    返回与估计 M 相距 ≤ tol 的骨架类别列表。

    :param m_est: estimate_molecular_weight 的结果
    :param reduction_label: NaBD4 / NaBH4 / 其它（未知时按 NaBD4 表门控，
            但调用方仍应依据 LABEL_UNKNOWN 压低证据上限，不得跨标签确认）
    :param tol: 容差（m/z）；校准缺失时应放宽（修改方案 4.6）
    """
    table = SKELETON_MW.get(reduction_label) or SKELETON_MW["NaBD4"]
    return [sk for sk, mw in table.items() if abs(mw - m_est) <= tol]


def gate_skeleton(skeleton: str | None, m_est: int | None,
                  reduction_label: str, tol: float) -> bool:
    """
    判断某骨架是否通过分子量门控。

    - m_est 为 None（高质量区信息不足）→ 放行（不为缺失信息误杀）
    - skeleton 未知 → 放行（让下游按其它证据处理）
    - 否则仅当骨架在 allowed_skeletons 内才放行
    """
    if m_est is None or not skeleton:
        return True
    return skeleton in allowed_skeletons(m_est, reduction_label, tol)
