# -*- coding: utf-8 -*-
"""
websocket.agent.ai_reply_engine 的评测 mock 注入（POL-007）
==========================================================
本模块仅作占位说明：runner 通过 ``generate_reply(..., client=llm_client)`` 注入
假 LLM 客户端实现 mock-llm 档，而无需真实网络。假客户端定义见
``tools/agent_eval/cli.py`` 中的 ``_FakeLLMClient``（按脚本回复 / 触发工具调用 /
模拟超时三模式）。
"""
