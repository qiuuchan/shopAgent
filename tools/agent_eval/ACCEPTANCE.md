# 评测模块验收（POL-009）——演示口径

> 本文件记录评测模块（POL-007/008/009）的**演示验收**。在无真实 LLM 配置、
> 无真实店铺知识数据的条件下，用 **mock 档**生成三份报告并跑 baseline vs
> candidate 对比，证明评测管线（检索 → 生成回复 → 指标聚合 → judge → 对比）
> 可用且**确定性**。**非真实 LLM / 知识数据评测**，真实档遗留项见文末。

## 交付物（tools/agent_eval/reports/）

| 文件 | 说明 |
| --- | --- |
| `report_baseline.json/.md` | 纯关键词检索档（mock） |
| `report_candidate.json/.md` | 混合检索档（mock，语义补齐 baseline 漏检） |
| `report_candidate_with_judge.json/.md` | candidate 档 + judge 平均分 |
| `judge_scores.json` | LLM-as-judge 平均分 |
| `compare_baseline_vs_candidate.json` | baseline vs candidate 对比 |

## 演示结果

| 指标 | baseline（纯关键词） | candidate（混合检索） | 差值 |
| --- | --- | --- | --- |
| 检索 hit@k | 0.00 | 1.00 | **+1.00** |
| AI 回退率 | 0.00 | 0.00 | 0.00 |
| 关键词合规率 | 1.00 | 1.00 | 0.00 |

- **回归明细**：0 条（candidate 未把 baseline 命中的 query 弄坏，无回归）。
- **judge 均分**：准确性 5.0 / 相关性 4.0 / 合规性 5.0（仿真打分）。

## 结论

- 评测管线端到端可用：数据集加载 → 逐条检索 + 生成回复 → 指标聚合（hit@k /
  回退率 / 合规率 / 延迟）→ JSON + Markdown 报告 → judge 打分 → baseline vs
  candidate 对比，全部落盘。
- **确定性**：`--mock-llm` 档两次运行报告逐字节一致（已在 `test_agent_eval.py`
  断言进测试，CI 可跑）。演示中 baseline 命中 0 与 candidate 命中 1 的差异，
  由 mock 检索后端的语义补齐驱动，直观展示混合检索的增益。

## 遗留项（真实档待满足）

1. **无真实 LLM 配置**：库中 `pdd_llm_config` 为 0 条，.env 无 key；真实 `--real`
   档与 `--judge` 档需配置店铺 LLM 后方可执行。
2. **无真实店铺知识数据**：`pdd_customer_service_knowledge` / `pdd_product_knowledge`
   均 0 条，无可检索数据。
3. 真实评测需：真实知识 backfill → 真实/仿真问题集 → 三档报告（mock / real /
   judge）→ 对比。当前环境不可行，故以演示口径 + 确定性断言替代关闭。
