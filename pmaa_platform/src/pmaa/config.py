"""
PMAA 甲基化分析平台 —— 全局常量与版本定义。

版本号进入 ReportSnapshot / EvidenceBundle，保证报告可重放：
同一 (input_hash, library_version, rule_version, method_version) 必得同一结果。
"""
from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------- 路径
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.environ.get("PMAA_DATA_DIR", PROJECT_ROOT / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_DB_URL = os.environ.get("PMAA_DB_URL", f"sqlite:///{DATA_DIR / 'pmaa.db'}")

# 上传文件存放区：保存原件（重命名防路径穿越），按内容哈希去重
UPLOAD_DIR = Path(os.environ.get("PMAA_UPLOAD_DIR", DATA_DIR / "uploads"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------- 上传限制
# 压缩包一律拒收：不解压、不展开（开发计划第 6 节要求限制压缩包展开）。
MAX_UPLOAD_BYTES = int(os.environ.get("PMAA_MAX_UPLOAD_BYTES", 50 * 1024 * 1024))
REJECTED_EXTENSIONS = {".zip", ".rar", ".7z", ".gz", ".tar", ".bz2", ".xz"}

# 支持矩阵：单一数据源，前后端共用（GET /v1/imports/formats 直接返回本表）
SUPPORTED_UPLOAD_FORMATS: list[dict] = [
    {
        "extension": ".xlsx",
        "mime": ["application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"],
        "kind": "ei_reference_table",
        "label": "PMAA 的 EI 质谱汇总表（Excel 工作簿）",
        "parser": "ingest/xlsx_reader.py",
        "notes": "只读解析；识别合并单元格、字体/填充颜色、隐藏行列与注释行；"
                 "每个取值保留源单元格坐标。不执行宏。",
    },
    {
        "extension": ".xlsm",
        "mime": ["application/vnd.ms-excel.sheet.macroEnabled.12"],
        "kind": "ei_reference_table",
        "label": "启用宏的 Excel 工作簿",
        "parser": "ingest/xlsx_reader.py",
        "notes": "按数据读取，宏一律不执行。",
    },
    {
        "extension": ".csv",
        "mime": ["text/csv", "application/csv"],
        "kind": "peak_table",
        "label": "已积分峰表（逗号分隔）",
        "parser": "ingest/peak_table.py",
        "notes": "需要表头：component_id, rt, area, evidence_type, ions 等。",
    },
    {
        "extension": ".tsv",
        "mime": ["text/tab-separated-values"],
        "kind": "peak_table",
        "label": "已积分峰表（制表符分隔）",
        "parser": "ingest/peak_table.py",
        "notes": "同 CSV，分隔符为制表符。",
    },
    {
        "extension": ".json",
        "mime": ["application/json"],
        "kind": "peak_table",
        "label": "峰表 / 结构化输入（JSON）",
        "parser": "ingest/peak_table.py",
        "notes": "接受 {\"components\":[...]} 或顶层数组。",
    },
    {
        "extension": ".pdf",
        "mime": ["application/pdf"],
        "kind": "document",
        "label": "PDF 文档（谱图数据表 / 谱图）",
        "parser": "ingest/doc_parser.py",
        "notes": "优先提取文本与表格；无文本时渲染页面走图像反解，需人工校准 m/z 轴。",
    },
    {
        "extension": ".docx",
        "mime": ["application/vnd.openxmlformats-officedocument.wordprocessingml.document"],
        "kind": "document",
        "label": "Word 文档（.docx）",
        "parser": "ingest/doc_parser.py",
        "notes": "读取表格与正文；无表格时取内嵌图片走图像反解。",
    },
    {
        "extension": ".doc",
        "mime": ["application/msword", "application/CDFV2"],
        "kind": "document",
        "label": "老版 Word 二进制文档（.doc）",
        "parser": "ingest/doc_parser.py",
        "notes": "优先用本地 LibreOffice 无界面转换为 .docx；macOS 可用 textutil 作为仅文本兜底。"
                 "转换和提取结果均标为推导数据。",
    },
    {
        "extension": ".png",
        "mime": ["image/png"],
        "kind": "spectrum_image",
        "label": "质谱图（PNG）",
        "parser": "ingest/spectrum_image.py",
        "notes": "反解棒图/填充峰。强度是像素近似值，m/z 需人工校准轴范围并逐峰核对。",
    },
    {
        "extension": ".jpg",
        "mime": ["image/jpeg"],
        "kind": "spectrum_image",
        "label": "质谱图（JPEG）",
        "parser": "ingest/spectrum_image.py",
        "notes": "同 PNG；有损压缩可能带来少量伪峰。",
    },
    {
        "extension": ".jpeg",
        "mime": ["image/jpeg"],
        "kind": "spectrum_image",
        "label": "质谱图（JPEG）",
        "parser": "ingest/spectrum_image.py",
        "notes": "同 JPEG。",
    },
    {
        "extension": ".tif",
        "mime": ["image/tiff"],
        "kind": "spectrum_image",
        "label": "质谱图（TIFF）",
        "parser": "ingest/spectrum_image.py",
        "notes": "扫描件常用，无压缩伪影，优于 JPEG。",
    },
    {
        "extension": ".tiff",
        "mime": ["image/tiff"],
        "kind": "spectrum_image",
        "label": "质谱图（TIFF）",
        "parser": "ingest/spectrum_image.py",
        "notes": "同 TIFF。",
    },
    {
        "extension": ".webp",
        "mime": ["image/webp"],
        "kind": "spectrum_image",
        "label": "质谱图（WebP）",
        "parser": "ingest/spectrum_image.py",
        "notes": "同 PNG。",
    },
    {
        "extension": ".bmp",
        "mime": ["image/bmp"],
        "kind": "spectrum_image",
        "label": "质谱图（BMP）",
        "parser": "ingest/spectrum_image.py",
        "notes": "无压缩位图。",
    },
]

SUPPORTED_EXTENSIONS: set[str] = {f["extension"] for f in SUPPORTED_UPLOAD_FORMATS}

# 文档类：走"提取 + 人工校准"流程，而不是直接入库
DOCUMENT_EXTENSIONS: set[str] = {".pdf", ".docx", ".doc"}
# 图像类：必须人工校准 m/z 轴
SPECTRUM_IMAGE_EXTENSIONS: set[str] = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".bmp"}
# 仍不支持的旧格式；.doc 已有受控转换流程。
LEGACY_DOC_EXTENSIONS: set[str] = {".rtf", ".wps"}

# 图像反解的建议 m/z 轴范围——只用于占位提示，界面不得自动提交为校准值。
DEFAULT_MZ_RANGE = (40.0, 350.0)

# ---------------------------------------------------------------- 版本
RULE_VERSION = "rules_v1.1.0"
PROMPT_VERSION = "prompt_v1.0.0"
PARSER_VERSION = "xlsx_parser_v1.0.0"
LLM_SCHEMA_VERSION = "1.0"
IMPORT_SCHEMA_VERSION = "import_v1.0.0"

# ---------------------------------------------------------------- 定性离子表的证据上限
# 依据 03 契约 A01/A23：定性离子表不得生成数值强度、不得跑全谱余弦，
# 且不可能满足"完整 EI + 同方法标准 + 保留"的确认条件，因此上限为 candidate。
QUALITATIVE_EVIDENCE_CEILING = "candidate"

# ---------------------------------------------------------------- 检索默认参数（工程可调）
SEARCH_DEFAULTS = {
    "top_k": 5,
    "qualitative_red_boost": 2.5,      # 原表标红（>20%，即基峰/强峰）离子的权重增益；
                                       # 修改方案 P2：放大强峰主导，近似强度加权召回
    "qualitative_idf_cap": 4.0,        # 区分力权重上限，避免稀有离子独大
    "qualitative_min_score": 0.0,      # 不设硬阈值；只排序，不做确认
    # 未经标准集标定的保守警戒线：低于此值仍展示候选，但整体身份强制为 unknown，
    # 防止“库中没有正确条目”时把相对最高分误读为可靠候选。
    "qualitative_low_match_warning": 0.30,
    # 加权几何均值中"查询被解释比例"的指数。取 >0.5 是有意的：
    # 定性表只报告少量离子，若让参考覆盖率主导，离子数少的条目会靠"分母小"刷分，
    # 导致无法解释全部观测离子的条目排到前面。
    "qualitative_recall_weight": 0.7,
    # 参考表红色只表示“该参考谱中相对强度 >20%”，不是唯一 signature。
    # 只有“仪器直接导出的完整峰表”才允许把未见参考强峰当作软性负证据；
    # selected_ions、图片/文档反解一律不施加该惩罚（A02：未列出不等于未检出）。
    "qualitative_missing_strong_penalty": 0.15,
    # 评分模式：geometric（默认，precision 权重更高，避免小分母刷分）| f1（对称 F1，
    # 库离子收录多少不主导排名）。修改方案 P2：两模式并列可 A/B，不改默认不回归。
    "scoring_mode": "geometric",
    # 参考条目离子数 < 此值标记为低覆盖（P3）：该条目排名对上传完整度较敏感，
    # 需在候选上给 ⚠ 提示并建议补全参考库 / 补 RI/NMR。
    "low_coverage_ref_ion_threshold": 8,
    "numeric_mz_exponent": 0.5,        # 加权余弦 m/z 指数
    "numeric_int_exponent": 0.5,       # 加权余弦 强度指数
    "near_tie_delta": 0.05,            # 未标定检索分数的保守近邻带；近邻只作并列候选（A13）
    # 旧版实验性分子量启发式的保留参数；生产流水线不再调用。
    # PMAA 分子量随甲基/乙酰取代数变化，不能按 Hex/dHex/Pen 各设一个固定 MW 硬门控。
    "mw_gate_tolerance_calibrated": 2.0,
    "mw_gate_tolerance_uncalibrated": 4.0,
    # 门控后仍并列（Top1/Top2 分差 < 该值）且物理上仍可能混淆时，
    # 强制要求第二根柱/标准品复核，不得只留 Top1（修改方案 P3/P4 / A13）。
    "mw_gate_near_tie_delta": 0.03,
    # 保留指数（RI）定种通道容差（数据驱动，无数据则 no-op）：
    # ri_tolerance = 精确容差（唯一标准落此内即定种）；
    # ri_wide_tolerance = 放宽容差（超出精确容差但最近邻落此内 → 最近邻告警）。
    "ri_tolerance": 15.0,
    "ri_wide_tolerance": 30.0,
}

# ---------------------------------------------------------------- LLM 运行时默认值（04 契约）
LLM_RUNTIME_DEFAULTS = {
    "request_timeout_seconds": 60,
    "sample_total_budget_seconds": 180,
    "per_sample_concurrency": 2,
    "max_transport_retries": 2,
    "max_schema_repair_attempts": 1,
    "estimated_component_input_token_budget": 8000,
    "estimated_component_output_token_budget": 2000,
    "hard_cost_limits_required_for_batch": True,
    "raw_sample_file_transfer_default": False,
    "save_response_for_replay": True,
}

# ---------------------------------------------------------------- 缺失证据代码表（封闭集合）
MISSING_EVIDENCE_CODES = {
    "RING_FORM_EVIDENCE",        # 原残基环型无独立证据
    "FULL_EI",                   # 只有定性离子集合，无数值全谱
    "RETENTION_EVIDENCE",        # 无同方法保留观测
    "METHOD_COMPATIBILITY",      # 参考方法与本次方法不兼容/未桥接
    "LABEL_UNKNOWN",             # NaBD4/NaBH4 标签未知
    "CALIBRATION_MISSING",       # 无校准，不能给 mol%
    "STANDARD_SAME_METHOD",      # 缺同方法真实标准
    "COMPOSITION_METHOD_LIMIT",  # 单糖组成检出能力未知
    "COELUTION_UNRESOLVED",      # 共洗脱不可辨识
    "AMINO_SUGAR_BRANCH",        # 氨基糖专项分支未验证
    "IMAGE_DERIVED",             # 数据由图片/文档反解得到，不是仪器直接导出
    "MANUAL_CALIBRATION_PENDING",# 图像反解结果尚未经人工校准确认
    "LIBRARY_COVERAGE_LOW",      # 最高匹配仍很低，疑似库外/轴错位/提取伪峰
}
