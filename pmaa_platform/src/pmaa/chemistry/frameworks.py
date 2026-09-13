"""
糖残基骨架框架定义。

对应 03 契约 mapping_frameworks 与 14.6.1：
  a = 原异头碳； r = 原成环氧所连的非异头碳； U = 可具有游离羟基或受体连接的碳位集合。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Framework:
    id: str
    skeleton: str            # Hex / Pen / dHex
    ring_form: str           # p / f
    anomeric_position: int   # a
    ring_closure_position: int  # r
    available_positions: tuple[int, ...]  # U
    note: str = ""

    def available_after_n_substitution(self, n_positions: frozenset[int]) -> tuple[int, ...]:
        """
        氨基糖修正：N 取代碳位（如 GlcNAc 的 C2）不是"游离羟基或受体连接位"，
        必须从 U 中剔除，否则会误把 NAc 位解释成糖苷连接位（14.6/14.4.2）。
        """
        return tuple(p for p in self.available_positions if p not in n_positions)


FRAMEWORKS: dict[str, Framework] = {
    "aldohexopyranose": Framework(
        "aldohexopyranose", "Hex", "p", 1, 5, (2, 3, 4, 6), "Glcp/Galp/Manp 等"),
    "aldohexofuranose": Framework(
        "aldohexofuranose", "Hex", "f", 1, 4, (2, 3, 5, 6), "环型不同，缺甲基位置解释改变"),
    "aldopentopyranose": Framework(
        "aldopentopyranose", "Pen", "p", 1, 5, (2, 3, 4), "Xylp/Arap 等"),
    "aldopentofuranose": Framework(
        "aldopentofuranose", "Pen", "f", 1, 4, (2, 3, 5), "Araf/Xylf 等"),
    "six_deoxy_aldohexopyranose": Framework(
        "six_deoxy_aldohexopyranose", "dHex", "p", 1, 5, (2, 3, 4),
        "Fucp/Rhap 的 C6 不能作为普通 O-Me 或 O 连接位"),
}

# 糖名 → 骨架类别。未知糖名返回 None，调用方必须显式处理（不猜）。
_SUGAR_TO_SKELETON: dict[str, str] = {}
for _name in ("Glc", "Gal", "Man", "Gul", "Alt", "All", "Ido", "Tal", "GulNAc", "GlcNAc", "GalNAc", "ManNAc"):
    _SUGAR_TO_SKELETON[_name] = "Hex"
for _name in ("Ara", "Xyl", "Rib", "Lyx"):
    _SUGAR_TO_SKELETON[_name] = "Pen"
for _name in ("Fuc", "Rha", "Qui"):
    _SUGAR_TO_SKELETON[_name] = "dHex"

# 常见环型先验：Ara/Xyl 在数据表中按呋喃糖统计，但映射时仍需保留吡喃型候选（14.6.4）
DEFAULT_RING_PRIOR: dict[str, str] = {"Ara": "f", "Xyl": "f"}


def skeleton_of(sugar: str | None) -> str | None:
    if not sugar:
        return None
    return _SUGAR_TO_SKELETON.get(sugar)


def frameworks_for_skeleton(skeleton: str | None) -> list[Framework]:
    if skeleton is None:
        return list(FRAMEWORKS.values())
    return [f for f in FRAMEWORKS.values() if f.skeleton == skeleton]


def get_framework(fid: str) -> Framework:
    if fid not in FRAMEWORKS:
        raise KeyError(f"未知骨架框架: {fid}")
    return FRAMEWORKS[fid]
