# LangGraph 决策链 spike 报告（POL-010）

> 本 spike 用 LangGraph StateGraph 重写主项目「拼多多自动回复」的 **9 级短路
> 自动回复决策链**（`websocket/engine/reply_engine.decide_reply`），与自研顺序
> 短路实现**双跑对拍**，论证「主项目保持自研」的依据。这也是把「为什么自研
> Agent 循环」从防守话术转成主动卖点的技术验证。
>
> **spike 边界**：仅存在于 `spike/langgraph_decision_chain/`，用独立 venv
> （仅装 langgraph + tzdata），**不打进主 pyproject、不影响主项目四服务测试**。
> `langgraph_decision_chain.py` / `dual_run.py` 只读复用 `websocket.engine.*` 的
> 纯逻辑判定（黑名单/过滤/营业时间/风控/关键词/商品专属），差异仅在**编排方式**。

---

## 1. 结论（先说干货）

**主项目继续自研 `decide_reply`，不引入 LangGraph。**

依据（下文量化）：对同样 17 例覆盖全部 9 级优先级与边界变体的用例，自研与
LangGraph 版决策结果**逐字段一致**（action / log_result / content / reply_type /
should_reply / matched_rule_id）。但 LangGraph 版需要引入 **37 个传递依赖包**
（含 langchain-core / langsmith / langgraph-checkpoint / -prebuilt / -sdk 等），
而自研实现零额外依赖（仅依赖标准库 + 项目内纯逻辑组件）；且自研是**纯函数、
无框架接线**，可直接单测 / 属性测试，LangGraph 版需理解框架的状态机语义。

| 维度 | 自研 `decide_reply` | LangGraph 版 |
| --- | --- | --- |
| 有效代码行（去注释/空行/docstring） | ~331 行（decision 逻辑） | ~272 行（本 spike 复刻） |
| 决策语义对拍 | 基准（oracle） | 17 例全部一致 |
| 部署依赖 | **0 新增**（标准库 + 项目纯逻辑） | **37 个**传递包（langchain-core 等） |
| 可测性 | 纯函数，直接单测/属性测试 | 需经状态机 invoke，测试耦合框架 |
| 控制力 / 可读性 | 顺序 if 短路，一眼可见优先级 | 显式节点 + 条件边，多一层抽象 |
| 故障可诊断 | 堆栈直达判定逻辑 | 需理解图的编译 / 路由中间层 |

---

## 2. 双跑对拍结果

用例来源：`websocket/tests/test_reply_engine.py`（抽提出涵盖 Property 13
「自动回复决策优先级链」全部 9 级与边界变体的 **17 例**）。

| 优先级/场景 | 自研 action | LangGraph action | 一致 |
| --- | --- | --- | --- |
| 黑名单短路 | blacklisted | blacklisted | ✅ |
| 过滤 > 关键词 | filtered | filtered | ✅ |
| 非营业时间 > 关键词 | off_hours | off_hours | ✅ |
| 营业时间内继续 | keyword | keyword | ✅ |
| 风控达上限 > 关键词 | risk_blocked | risk_blocked | ✅ |
| 风控未达上限继续 | keyword | keyword | ✅ |
| 关键词 > 商品专属/AI/默认 | keyword | keyword | ✅ |
| 关键词图片回复 | keyword | keyword | ✅ |
| 商品专属 > AI/默认 | goods_specific | goods_specific | ✅ |
| 商品专属 miss → 默认 | default | default | ✅ |
| AI > 默认 | ai | ai | ✅ |
| 默认回复兜底 | default | default | ✅ |
| 无匹配（no_match） | no_match | no_match | ✅ |
| 空默认视为未配置 | no_match | no_match | ✅ |
| 默认只回一次·已发送跳过 | no_match | no_match | ✅ |
| 默认只回一次·未发送发送 | default | default | ✅ |
| 未开只回一次·有记录仍发 | default | default | ✅ |

**17 / 17 全部一致**（对拍脚本：`dual_run.py`，退出码 0）。
验证口径：对每条用例，两实现 decision 的 `action / log_result / content /
reply_type / should_reply / matched_rule_id` 六字段逐一相等。

---

## 3. 实现要点（LangGraph 版如何复刻短路）

- **节点** = 9 级优先级各自一个纯函数：`node_blacklist / node_filter /
  node_business_hours / node_risk / node_keyword / node_goods_specific /
  node_ai / node_default / node_no_match`。
- **边** = 条件路由：节点命中（写 `state["decision"]`）→ `END`；未命中 → 进入
  下一优先级节点。`build_decision_graph()` 用 `StateGraph` + `add_conditional_edges`
  串联，`set_entry_point("blacklist")` 起点，`no_match` 为兜底终点。
- **复用纯逻辑**：`is_blacklisted / match_filter_rules / is_within_business_hours /
  check_reply_frequency / match_keyword / _match_goods_reply` 全部直接复用主项目
  `websocket.engine.*`，**零改动用例**——唯一差异是编排框架。

## 4. 依赖成本细账

spike venv（新建）安装 `langgraph` 后，`pip list` 共 **37** 个包（自venv已装
tzdata）。直接 / 传递相关：**langgraph 1.2.11、langgraph-checkpoint 4.2.0、
langgraph-prebuilt 1.1.0、langgraph-sdk 0.4.4、langchain-core 1.6.1、
langchain-protocol 0.0.19、langsmith 0.11.2**，其余为 pydantic / typing 扩展 /
缓存等传递依赖。这对一个「决策编排」功能而言偏重——主项目当前仅用 SQLAlchemy /
FastAPI / jieba，agent 循环完全自研，零 LangChain 家族依赖。

## 5. 「为什么自研」的面试口径（可直接讲）

1. **控制力**：9 级短路一目了然（顺序 if），LangGraph 需理解状态机 / 条件边
   抽象；出问题（如某次决策不收敛）自研的堆栈直达判定逻辑，图框架多一层路由。
2. **依赖成本**：一个决策编排引入 langchain-core 等 37 包，与主项目「仅标准库
   + 适配自有模型」的轻量取向冲突；多服务部署镜像、依赖升级面均被放大。
3. **可测性**：自研 `decide_reply` 是纯函数（无 I/O、无框架依赖），直接单测 /
   Hypothesis 属性测试；LangGraph 版测试需 `compiled.invoke(state)`，与框架绑定。
4. **满足需求**：本需求固定 9 级优先级，是最朴素的「顺序短路」结构——用图框架
   编排反而是「用大炮打蚊子」。LangGraph 的价值在**复杂循环 / 分支编排 / 持久化
   状态**，本项目未到该复杂度。（数据上：两实现 17 例输出完全一致。）
5. **学习价值**：本次 spike 完成了「自研 vs 框架」的**实证对比**，比空口说
   "框架太重"更有说服力。

## 6. 遗留项 / 未覆盖

- spike 未覆盖：营业时间跨午夜边界、风控的店铺维上限分支、黑名单 is_active=False
  的反例——这些在自研单测中已覆盖（websocket 352 例），spike 仅取代表性用例对拍。
- spike 未接入 CI（独立 venv），如需长期保留可考虑纳入定时对拍。

---

## 附：复现

```bash
# 在 spike 目录新建独立 venv（仅装 langgraph + tzdata，不打进主 pyproject）
python -m venv spike/langgraph_decision_chain/.venv
./spike/langgraph_decision_chain/.venv/Scripts/python -m pip install langgraph tzdata

# 双跑对拍（复用主项目 websocket.engine 纯逻辑）
./spike/langgraph_decision_chain/.venv/Scripts/python spike/langgraph_decision_chain/dual_run.py
```

输出：`17 / 17 一致（退出码 0）`。
