#!/usr/bin/env python3
"""
验收用例总入口。

    python tests/run_all.py

覆盖 03 数据契约 A01–A24 与 04 LLM 契约 B01–B16。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> int:
    import _harness
    _harness.fresh_db()

    import test_api  # noqa: F401  （需最后导入：它会在测试库里建数据）
    import test_chemistry  # noqa: F401
    import test_ingest  # noqa: F401
    import test_llm  # noqa: F401
    import test_qc  # noqa: F401
    import test_search_quant  # noqa: F401
    import test_methylation_revision  # noqa: F401  （甲基化判读修改方案回归）
    import test_retention_index  # noqa: F401  （保留指数 RI 定种通道骨架）
    import test_scoring_fairness  # noqa: F401  （评分公平性与参考库完整性：P1/P2/P3）

    print("=" * 72)
    print("PMAA 平台验收用例（03 数据契约 A01–A24 + 04 LLM 契约 B01–B16）")
    print("=" * 72)
    return _harness.SUITE.run()


if __name__ == "__main__":
    sys.exit(main())
