# 评测模块（tools/agent_eval）

> 本模块为「简历打磨工单池」批次 C 的工程载体（POL-007），实现 LLM 评测的
> golden 数据集 + 确定性指标 + baseline 对比。全部指标为纯函数，`--mock-llm`
> 档两次运行结果逐字节一致。

## 目录结构

```
tools/agent_eval/
├── dataset.py    # golden 数据集 JSONL 加载与校验
├── runner.py     # 评测执行核心（检索 + 生成回复，可注入 mock）
├── metrics.py    # 确定性指标纯函数（hit@k / 回退率 / 合规率 / 延迟分布）
├── report.py     # 报告生成（JSON + Markdown）
├── compare.py    # baseline vs candidate 报告对比（指标差分 + 回归明细）
├── cli.py        # 命令行入口（--mock-llm / --real）
├── datasets/
│   └── seed_shop.jsonl   # 种子数据集（20 例，多店铺电商客服场景）
└── README.md
```

## 运行

使用仓库根目录 `.venv`（common / websocket 已以可编辑方式安装）：

```bash
# mock-llm 档：确定性，两次运行结果一致（CI 可跑）
python tools/agent_eval/cli.py \
    --dataset tools/agent_eval/datasets/seed_shop.jsonl \
    --shop 1 --mode mock --out tools/agent_eval/reports

# 真实 LLM 档：按店铺 LlmConfig 构建
python tools/agent_eval/cli.py \
    --dataset tools/agent_eval/datasets/seed_shop.jsonl \
    --shop 1 --mode real --print
```

参数说明：

- `--dataset`：golden 数据集 JSONL 路径（必填）。
- `--shop`：店铺主键 shop_pk（必填，检索的店铺隔离参数）。
- `--mode`：`mock`（默认，确定性假 LLM）/ `real`（真实店铺 LLM 配置）。
- `--out`：报告输出目录；缺省则打印报告到 stdout。
- `--retrieval-limit`：检索结果上限（默认 5）。

## 数据集格式

`datasets/*.jsonl` 每行一个 JSON 对象，`#` 开头为注释行（运行时跳过）：

```json
{"id": "kb-001", "query": "你们支持七天无理由退换货吗",
 "expected_kb_titles": ["退换货政策"],
 "must_contain": ["七天无理由"], "must_not_contain": ["不支持"],
 "expect_fallback": false}
```

字段约定：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | 数据点唯一标识（必填） |
| `query` | string | 买家问题文本（必填） |
| `expected_kb_titles` | string[] | 期望检索命中的知识条目标题（可为空） |
| `must_contain` | string[] | AI 回复必须包含的子串（合规正向约束） |
| `must_not_contain` | string[] | AI 回复必须不含的子串（合规负向约束） |
| `expect_fallback` | bool | 是否期望触发 AI 回退（默认 false） |

## 编写指引（新增数据集）

1. 从店铺真实 / 仿真买家问题中挑选 20-30 条覆盖面广的 query：
   退换货 / 物流 / 运费 / 优惠 / 尺码 / 发票 / 售后 / 支付 / 营业时间 /
   正品保障等。
2. `expected_kb_titles` 填「期望检索 top-k 命中的知识标题」，与店铺知识库
   实际标题一致，避免误导评估。
3. `must_contain` / `must_not_contain`：用关键词约束回复合规性；建议同时覆盖
   正例（必须包含）与反例（必须不含，如「不支持」「加钱」）。
4. `expect_fallback`：对确实无相关知识的问题置 `true`，检验 AI 诚实回退。
5. 每类问题至少覆盖 2-3 条，便于 hit@k 有区分度。

## 指标定义

全部指标值 ∈ [0, 1]（延迟分位值为毫秒）：

- **检索 hit@k**：`expected_kb_titles` 是否有任一出现在检索 top-k 结果。
- **AI 回退率**：`used_default=True` 的用例占比。
- **关键词合规率**：`must_contain` 全中且 `must_not_contain` 全不中的用例占比。
- **延迟分布**：p50 / p95（毫秒，升序插值）。

## 确定性保证

`--mode mock` 注入假 LLM 客户端（按脚本复述 query），避免真实网络抖动。
两次运行产出同一 `report.json`（逐字节一致），可作为回归门禁进 CI。

## 对比（baseline vs candidate）

```bash
python -c "from tools.agent_eval.compare import compare_reports; import json;
base=json.load(open('tools/agent_eval/reports/base.json'));
cand=json.load(open('tools/agent_eval/reports/cand.json'));
print(compare_reports(base, cand))"
```
