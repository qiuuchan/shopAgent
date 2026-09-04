# -*- coding: utf-8 -*-
"""
tools.agent_eval.cli —— 评测 CLI 入口（--mock-llm 档）
=====================================================
本文件用途：提供评测模块（POL-007）的命令行入口，遍历数据集、执行检索 +
AI 回复生成、产出 JSON / Markdown 报告。

用法（使用仓库根目录 .venv，common / websocket 已以可编辑方式安装）：

    python tools/agent_eval/cli.py --dataset tools/agent_eval/datasets/seed_shop.jsonl \
        --shop 1 --mock-llm --out tools/agent_eval/reports
    python tools/agent_eval/cli.py --dataset ... --shop 1 --real        # 真实 LLM 档
    python tools/agent_eval/cli.py --dataset ... --shop 1 --mock-llm --print

确定性保证：``--mock-llm`` 档注入假 LLM 客户端（按脚本回复），两次运行
逐字节一致（两次运行结果与首次一致，供 CI 回归）。

mock-llm 假客户端接口：与 ``generate_reply(client=...)`` 契约一致——``chat``
返回带 ``content`` / ``tool_calls`` 的 LLMResponse 对象，``tools`` 属性可为空。
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Any, List, Optional

# 保证 tools / common / websocket 可从仓库根导入（与 tools 测试 conftest 一致）。
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from common.db.session import get_session_factory  # noqa: E402
from tools.agent_eval.dataset import GoldenCase, iter_cases  # noqa: E402
from tools.agent_eval.report import build_report, to_json, to_markdown  # noqa: E402
from tools.agent_eval.runner import run_all  # noqa: E402


class FakeLLMClient:
    """刻意简化的假 LLM 客户端：按预设脚本回复（确定性）。

    ``generate_reply(client=...)`` 会写入 ``client.tools``；本类提供该属性。
    ``chat`` 直接返回带预设内容（或触发工具调用 / 模拟超时）的响应，用于
    mock-llm 档确定性评测。
    """

    def __init__(self, mode: str = "reply") -> None:
        self.tools: List[Any] = []
        self._mode = mode

    async def chat(self, messages, tool_choice: str = "auto"):
        # 延迟分布以固定值构造确定性（避免真实时钟抖动）：返回固定延迟说明。
        class _Resp:
            content: Optional[str] = None
            tool_calls: List[Any] = []

            @property
            def has_tool_calls(self) -> bool:
                return bool(self.tool_calls)

        resp = _Resp()
        if self._mode == "timeout":
            raise TimeoutError("mock timeout")
        if self._mode == "tool":
            # 触发一次工具调用（模型请求检索），然后由引擎二次调用返回文本。
            resp.tool_calls = [{"id": "call_1", "type": "function", "function": {"name": "search_customer_service_knowledge", "arguments": "{}"}}]
        # 默认直接答复：复述 query 以便命中 must_contain（评测口径）。
        last_user = messages[-1].get("content") if messages else ""
        resp.content = f"您好，我来为您解答：{last_user}"
        return resp


def build_parser() -> argparse.ArgumentParser:
    """构造 CLI 参数解析器。"""
    parser = argparse.ArgumentParser(description="评测模块 CLI（POL-007）")
    parser.add_argument("--dataset", required=True, help="golden 数据集 JSONL 路径")
    parser.add_argument("--shop", type=int, required=True, help="店铺主键 shop_pk")
    parser.add_argument(
        "--mode",
        choices=["mock", "real"],
        default="mock",
        help="评测档位：mock（确定性假 LLM）/ real（真实店铺 LLM 配置）",
    )
    parser.add_argument("--out", default=None, help="报告输出目录")
    parser.add_argument("--print", action="store_true", help="打印报告到 stdout")
    parser.add_argument("--retrieval-limit", type=int, default=5, help="检索结果上限")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    cases: list[GoldenCase] = iter_cases(args.dataset)
    if not cases:
        print("数据集无有效用例")
        return 1

    session_factory = get_session_factory()
    llm_client = FakeLLMClient(mode="reply") if args.mode == "mock" else None

    # 生成回复函数与运行时配置：mock 档需有效 config（ai_enabled + 非空 key）
    # 才能走通 generate_reply 的 LLM 分支（否则因配置无效直接回退默认回复）。
    try:
        from websocket.agent import ai_reply_engine
        from websocket.agent.agent_config import AgentConfig

        generate_reply = ai_reply_engine.generate_reply
        if args.mode == "mock":
            agent_config = AgentConfig(
                model_name="mock-model",
                api_key="sk-mock",  # 占位，实际经 llm_client 注入不触网络
                api_base="https://mock.example.com/v1",
                temperature=0.3,
                max_loops=3,
                timeout_seconds=5.0,
                ai_enabled=True,
            )
        else:
            agent_config = None  # 真实档：generate_reply 内部按店铺 config 构建
    except Exception as exc:  # noqa: BLE001 - websocket 未装时给出明确提示
        print(f"无法导入 websocket 生成器：{exc}")
        return 1

    with session_factory() as session:
        outcomes = run_all(
            cases,
            session=session,
            llm_client=llm_client,
            shop_id=args.shop,
            config=agent_config,
            default_reply="默认回复：我们的客服会尽快为您处理。",
            kb_search=None,  # 使用默认 kb_service.search
            generate_reply=generate_reply,
            retrieval_limit=args.retrieval_limit,
            record_latency=(args.mode != "mock"),  # mock 档固定延迟保证逐字节一致
        )

    report = build_report(outcomes)
    if args.print or not args.out:
        print(to_markdown(report))
    if args.out:
        os.makedirs(args.out, exist_ok=True)
        json_path = os.path.join(args.out, "report.json")
        md_path = os.path.join(args.out, "report.md")
        with open(json_path, "w", encoding="utf-8") as fh:
            fh.write(to_json(report))
        with open(md_path, "w", encoding="utf-8") as fh:
            fh.write(to_markdown(report))
        print(f"报告已生成：{json_path} / {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
