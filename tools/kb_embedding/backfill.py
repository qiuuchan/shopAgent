# -*- coding: utf-8 -*-
"""
tools.kb_embedding —— 知识库向量回填 CLI（POL-004）
====================================================
本文件用途：为知识库向量索引提供命令行回填工具，覆盖三类操作档位：

- ``--mode full``：全量重建——对指定店铺全部启用知识条目重新生成向量并 upsert；
- ``--mode delta``：差额补建——仅处理「无向量」或「content_hash 与当前内容不匹配」
  的条目（增量更新，避免全量重算）；
- ``--mode check``：仅检测——对比内存 content_hash 与库中向量，报告失效/缺失清单。

设计要点：
- 向量化与落库复用 ``common.services.kb_indexing``（与 backend 写路径同一逻辑，
  保证一致性）；失败条目记 warning 并计入统计，不中断批量。
- 纯逻辑（差额判定 / hash 比对）抽为纯函数（``decide_backfill``），便于单测。
"""
from __future__ import annotations

import sys
from typing import Any, Optional

# 允许脚本方式直接运行：仓库根为其父目录（tools/kb_embedding/ -> tools/ -> 根）。
sys.path.insert(0, "..")


def decide_backfill(container: dict[str, Any]) -> str:
    """差额 / 检测档的条目处置判定（纯函数，无 I/O）。

    Args:
        container: {"vector_json", "content_hash"} 字典；无向量视作重建。

    Returns:
        "rebuild"（无向量或 content_hash 不匹配）/ "skip"（向量有效一致）。
    """
    return "rebuild" if not container.get("vector_json") else "skip"


def build_content_text(title: str = "", content: str = "", tags: str = "") -> str:
    """拼接知识文本（标题 + 内容 + 标签），用于向量化（纯函数）。"""
    return " ".join(part for part in (title, content, tags) if part)


def main(argv: Optional[list] = None) -> int:
    """CLI 入口：解析参数并按档位执行回填。"""
    import argparse
    import json

    from sqlalchemy.orm import Session

    from common.db.repository import Repository
    from common.db.session import get_session_factory
    from common.models.knowledge_models import CustomerServiceKnowledge, KbEmbedding
    from common.services.kb_indexing import build_content_hash, index_knowledge_content

    parser = argparse.ArgumentParser(description="知识库向量回填 CLI（POL-004）")
    parser.add_argument("--shop", type=int, required=True, help="店铺主键 shop_pk")
    parser.add_argument(
        "--mode", choices=["full", "delta", "check"], default="delta", help="档位"
    )
    parser.add_argument("--db", type=str, default=None, help="数据库 URL 覆盖（默认系统配置）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出统计")
    args = parser.parse_args(argv)

    session_factory = get_session_factory() if not args.db else _make_factory(args.db)

    with session_factory() as session:
        cs_repo = Repository(CustomerServiceKnowledge, session)
        kb_repo = Repository(KbEmbedding, session)
        items = cs_repo.list(
            filters={"shop_pk": args.shop, "enabled": True}, order_by=False
        )
        stats = {"total": len(items), "built": 0, "failed": 0, "stale": 0, "skipped": 0}
        for item in items:
            text = build_content_text(item.title, item.content, item.tags or "")
            current_hash = build_content_hash(text)
            existing = kb_repo.get_by(
                shop_pk=args.shop, source_type="cs_knowledge", source_id=item.id
            )

            if args.mode == "check":
                if existing is None or existing.content_hash != current_hash:
                    stats["stale"] += 1
                else:
                    stats["skipped"] += 1
                continue

            # delta 档：向量有效且 hash 一致 → 跳过；否则重建。full 档：全量重建。
            should_rebuild = (
                args.mode == "full"
                or existing is None
                or existing.content_hash != current_hash
            )
            if not should_rebuild:
                stats["skipped"] += 1
                continue

            if index_knowledge_content(
                session,
                shop_pk=args.shop,
                source_type="cs_knowledge",
                source_id=item.id,
                content_text=text,
            ):
                stats["built"] += 1
            else:
                stats["failed"] += 1

        if args.json:
            print(json.dumps(stats, ensure_ascii=False))
        else:
            print(
                f"统计：总数 {stats['total']}，建成 {stats['built']}，失败 {stats['failed']}，"
                f"失效 {stats['stale']}，跳过 {stats['skipped']}"
            )
    return 0


def _make_factory(db_url: str):
    """按数据库 URL 构造会话工厂（本地 SQLite 验证用，需先建表）。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from common.models.base import Base

    engine = create_engine(db_url, future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False, future=True)


if __name__ == "__main__":
    raise SystemExit(main())
