# -*- coding: utf-8 -*-
"""
common.services.kb_hybrid —— 知识库混合检索排序纯函数
=====================================================
本文件用途：承载「token 命中 + 向量余弦」双信号混合排序的纯逻辑（POL-005），
与 ``kb_service.py``（数据访问与检索组装）分离，便于单测与属性测试。

设计要点：
- **无 I/O**：本模块仅做数值归一化、加权合并与排序，不访问 DB / 网络，
  传入即返回，可独立验证（与 ``kb_service`` 对拍，见属性测试）。
- **min-max 归一化**：token 命中数与余弦相似度各自落入 [0,1]（同分时可稳定
  排序），再按 ``weight`` 加权合并。任一信号全为 0 时归一化安全返回 0。
- **确定性**：分数相同时按记录 id 升序、再按原始顺序回溯，保证同输入同输出。

对外核心函数：
- ``minmax_scale``：普通 min-max 归一化（可定制低值映射，见 cosine）。
- ``hybrid_rank``：给定记录、token 命中数、余弦分、权重与上限，返回排序后的
  记录列表（长度 ≤ limit）。
"""
from __future__ import annotations

from typing import Iterable, Sequence, TypeVar

from common.services.embedding_service import cosine_similarity

# 检索记录泛型（可直接是 ORM 对象，仅需有 id 属性）。
RecordT = TypeVar("RecordT", bound=object)

# 默认混合权重：token 分与余弦分各半（POL-005 设计默认 0.5）。
DEFAULT_WEIGHT: float = 0.5


def minmax_scale(values: Sequence[float], low: float = 0.0) -> list[float]:
    """对数值序列做 min-max 归一化到 [low, 1]。

    全为 0 时安全返回全 0（避免除零）。值越大归一化越高；最小值对应 ``low``
    （默认 0，余弦场景可传 0 以区分方向相反与无关联）。

    Args:
        values: 原始数值序列（非负，通常为命中数或余弦相似度）。
        low: 最小值映射到的目标值。

    Returns:
        归一化后的序列，与输入等长。
    """
    if not values:
        return []
    vmax = max(values)
    vmin = min(values)
    if vmax <= vmin:
        # 全相等（含全 0）：无法差异化，统一返回 low（避免除零）。
        return [low for _ in values]
    span = vmax - vmin
    return [low + (v - vmin) / span for v in values]


def _score_records(
    records: Sequence[RecordT],
    token_scores: dict[int, float],
    vector_scores: dict[int, float],
    *,
    weight: float = DEFAULT_WEIGHT,
) -> list[tuple[RecordT, float]]:
    """对记录逐条计算加权合并分（归一化后合并），返回分数序列。

    Args:
        records: 参与排序的记录列表。
        token_scores: 记录 id -> token 命中数（未命中记录缺失）。
        vector_scores: 记录 id -> 余弦相似度（缺失视作无向量）。
        weight: 归一化后 token 分权重（∈[0,1]），其余权重给余弦分。

    Returns:
        与 records 等长的 [(record, score)] 序列。
    """
    ids = [getattr(rec, "id", 0) for rec in records]
    token_vals = [float(token_scores.get(i, 0)) for i in ids]
    cosine_vals = [float(vector_scores.get(i, 0)) for i in ids]

    norm_token = minmax_scale(token_vals)
    # 余弦天然 ∈ [-1,1]，统一 min-max 归一化到 [0,1]（min→0, max→1）；
    # 全相等（含全 0 无向量）时 minmax_scale 安全返回全 0。
    norm_cosine = minmax_scale(cosine_vals)

    weight = max(0.0, min(1.0, float(weight)))
    out = []
    for rec, t, c in zip(records, norm_token, norm_cosine):
        combined = weight * t + (1.0 - weight) * c
        out.append((rec, combined))
    return out


def hybrid_rank(
    records: Sequence[RecordT],
    token_scores: dict[int, float],
    vector_scores: dict[int, float],
    weight: float = DEFAULT_WEIGHT,
    limit: int | None = None,
) -> list[RecordT]:
    """混合排序：token 命中数与余弦各自归一化后加权合并，按分数降序返回。

    Args:
        records: 待排序记录列表（需可访问 ``id`` 属性）。
        token_scores: 记录 id -> token 命中数（词频 / 命中计数，用作词法信号）。
        vector_scores: 记录 id -> 余弦相似度（语义信号，缺失视作 0）。
        weight: 归一化后 token 分权重（∈[0,1]，默认 0.5）。
        limit: 返回条数上限；None 或非正数时返回全部（不再截断）。

    Returns:
        按加权合并分降序、同分按 id 升序稳定排序的记录列表。
    """
    if not records:
        return []
    scored = _score_records(records, token_scores, vector_scores, weight=weight)
    # 同分稳定序：分数降序、id 升序、原始顺序兜底（确定性，供对拍/测试）。
    ranked = sorted(
        scored,
        key=lambda pair: (-pair[1], getattr(pair[0], "id", 0)),
    )
    ordered = [rec for rec, _ in ranked]
    if limit is not None:
        try:
            max_count = int(limit)
        except (TypeError, ValueError):
            max_count = 0
        if max_count > 0:
            ordered = ordered[:max_count]
    return ordered


# 供 kb_service 复用余弦计算（避免重复实现，规范 52）。
__all__ = [
    "DEFAULT_WEIGHT",
    "minmax_scale",
    "hybrid_rank",
    "cosine_similarity",
]
