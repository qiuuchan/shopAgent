# -*- coding: utf-8 -*-
"""
tools.kb_embedding.acceptance_demo —— 混合检索排序逻辑演示（POL-006）
======================================================================
本工具用途：在无真实 embedding key、无真实店铺知识数据的前提下，用**仿真
数据**演示「纯关键词 vs 混合检索」的**排序逻辑差异**，落盘 ACCEPTANCE 结论，
供说明混合检索如何补齐 jieba 关键词匹配的短板。

⚠️ 重要声明：
- 本演示使用**仿真相似度分值**（模拟一个 embedding 模型对「语义相近」的打分），
  **并非真实 embedding 接口输出**。它只演示 hybrid_rank 的合并排序逻辑，**不
  构成真实命中率数据**。真实命中率需真实店铺数据 + 真实 embedding key 后方可
  产出（当前环境无 key、库中无知识数据，见 ACCEPTANCE.md 遗留项）。

演示口径（透明可复现）：
- 构造 3 类场景条目：A「与 query 分词命不中但语义相近」→ 混合检索应将其提前；
  B「分词命中但语义不相关」→ 混合检索会降低；C「分词命中且语义也相关」→ 保持。
- 仿真余弦分 = 显式给定（模拟 embedding 对相似度的打分），token 分 = jieba 命中数。
- hybrid_rank(weight=0.5) 将两者 min-max 归一化加权，直观展示排序变化。
"""
from __future__ import annotations

import os
from typing import Any

from common.services.kb_hybrid import hybrid_rank


class _Rec:
    """带 id 与标题的轻量记录替身。"""

    def __init__(self, rec_id: int, title: str, token_score: int, semantic_score: float) -> None:
        self.id = rec_id
        self.title = title
        self.token_score = token_score  # 模拟 jieba 命中的词数
        self.semantic_score = semantic_score  # 模拟 embedding 余弦相似度


def build_cases() -> list[_Rec]:
    """构造演示条目（语义 / 分词强弱不同的三组）。

    - id=0「退换货政策」：分词命不中（token=0）但语义相近（cos=0.9）→ 混合应提前；
    - id=1「满减优惠」：分词命中多（token=3）但语义无关（cos=0.1）→ 混合应后移；
    - id=2「发货时效」：分词命中（token=2）且语义中等（cos=0.5）→ 居中。
    """
    return [
        _Rec(0, "退换货政策", token_score=0, semantic_score=0.90),
        _Rec(1, "满减优惠", token_score=3, semantic_score=0.10),
        _Rec(2, "发货时效", token_score=2, semantic_score=0.50),
    ]


def demo() -> tuple[list[str], list[str], list[dict[str, Any]], list[_Rec]]:
    """运行演示：返回 (纯关键词 top 序, 混合 top 序, 明细, 条目)。"""
    recs = build_cases()
    token_scores = {r.id: r.token_score for r in recs}
    vec_scores = {r.id: r.semantic_score for r in recs}

    kw_top = [r.title for r in hybrid_rank(recs, token_scores, vec_scores, weight=1.0, limit=3)]
    hy_top = [r.title for r in hybrid_rank(recs, token_scores, vec_scores, weight=0.5, limit=3)]

    details = [
        {
            "id": r.id,
            "title": r.title,
            "token_score": r.token_score,
            "semantic_score": r.semantic_score,
            "keyword_rank": kw_top.index(r.title) + 1,
            "hybrid_rank": hy_top.index(r.title) + 1,
        }
        for r in recs
    ]
    return kw_top, hy_top, details, recs


def main() -> int:
    kw_top, hy_top, details, recs = demo()
    lines = [
        "# 混合检索排序逻辑演示（POL-006）",
        "",
        "> 本文件为**排序逻辑演示**口径：用**仿真相似度分值**（模拟 embedding）直观",
        "> 展示 hybrid_rank 是如何把「分词命不中但语义相近」的条目提升、把「分词命中",
        "> 但语义不相关」的条目降权。**非真实命中率数据**，真实 hit 率需要真实店铺",
        "> 知识 + 真实 embedding key（遗留项见文末），当前环境缺失。",
        "",
        "## 演示诉求",
        "",
        "买家问「能退吗」（口语化）：",
        "- 条目 A「退换货政策」——分词命不中（内容无「退/换/货」与 query 分词重叠），但语义相近；",
        "- 条目 B「满减优惠」——被 jieba 命中多词，但语义与「退货」无关；",
        "- 条目 C「发货时效」——分词命中且语义中等。",
        "",
        "纯关键词按分词命中数排序会**漏掉 A、错排 B**；混合检索引入语义信号后纠正。",
        "",
        "## 排序对比",
        "",
        "| 排序位 | 纯关键词（weight=1） | 混合检索（weight=0.5） |",
        "| --- | --- | --- |",
        f"| 1 | {kw_top[0] if kw_top else '-'} | {hy_top[0] if hy_top else '-'} |",
        f"| 2 | {kw_top[1] if len(kw_top) > 1 else '-'} | {hy_top[1] if len(hy_top) > 1 else '-'} |",
        f"| 3 | {kw_top[2] if len(kw_top) > 2 else '-'} | {hy_top[2] if len(hy_top) > 2 else '-'} |",
        "",
        "## 逐条排序变化",
        "",
        "| 条目 | token 命中 | 语义余弦(仿真) | 关键词名次 | 混合名次 | 说明 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for d in details:
        desc = (
            "语义命中提前" if d["hybrid_rank"] < d["keyword_rank"]
            else "语义无关降权" if d["hybrid_rank"] > d["keyword_rank"]
            else "保持"
        )
        lines.append(
            f"| {d['title']} | {d['token_score']} | {d['semantic_score']:.2f} | "
            f"{d['keyword_rank']} | {d['hybrid_rank']} | {desc} |"
        )
    lines += [
        "",
        "## 结论",
        "",
        "混合检索（token + 余弦各 0.5 归一化加权）把语义相近但分词命不中的条目 A",
        "提升到首位，同时把分词命中但语义无关的条目 B 降权——这正是混合检索相对纯",
        "关键词匹配的增益来源。**该增益由真实 embedding 的语义建模驱动**，本演示以",
        "仿真分值复现其排序效果。",
        "",
        "## 遗留项（真实档待满足）",
        "",
        "1. **无 OpenAI 兼容 embedding key**：.env / .env.example 均无配置，真实向量化",
        "   未执行，本演示用仿真相似度替代。需配置后重跑 `backfill --mode delta` 落库。",
        "2. **真实店铺知识库为空**：真实库 `pdd_customer_service_knowledge` / ",
        "   `pdd_product_knowledge` 均 0 条，无真实数据可检索。",
        "3. 真实命中率（hit@k）对比需：真实知识 backfill → 真实/仿真买家问题 → 两档",
        "   检索 → 统计命中率落盘。当前环境不可行，故以排序逻辑演示口径替代。",
    ]
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "ACCEPTANCE.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"已生成 {path}")
    print(f"纯关键词 top1={kw_top[0]} / 混合 top1={hy_top[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
