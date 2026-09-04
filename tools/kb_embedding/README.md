# 知识库向量回填工具（tools/kb_embedding）

> 本工具为「简历打磨工单池」批次 B 的索引构建侧（POL-004），为知识库内容
> 变更后的向量索引提供命令行回填能力，与 backend 写路径的 best-effort 挂钩
> 共用同一套向量化逻辑（`common.services.kb_indexing`）。

## 运行

使用仓库根目录 `.venv`（common 已以可编辑方式安装）：

```bash
# 差额补建（默认）：仅处理无向量或内容哈希不一致的知识条目
python tools/kb_embedding/backfill.py --shop 3

# 全量重建：对该店铺全部启用知识条目重新生成向量
python tools/kb_embedding/backfill.py --shop 3 --mode full

# 仅检测：报告缺失 / 失效清单，不做任何写入
python tools/kb_embedding/backfill.py --shop 3 --mode check --json
```

参数说明：

- `--shop`：店铺主键 shop_pk（必填）。
- `--mode`：`delta`（默认，差额补建）/ `full`（全量重建）/ `check`（仅检测）。
- `--db`：数据库 URL 覆盖（默认系统配置；本地无 MySQL 时可指定 sqlite 文件
  验证，需先建表：`python -c "import common.models.log_models; from common.models.base import Base; Base.metadata.create_all(e)"`）。
- `--json`：以 JSON 输出统计。

## 档位语义

| 档位 | 行为 | 输出 |
| --- | --- | --- |
| `--mode delta` | 对「无向量或 content_hash 与当前内容不一致」的条目重建向量；一致则跳过 | built / failed / skipped |
| `--mode full` | 对该店铺全部启用知识条目重建向量（忽略 hash） | built / failed |
| `--mode check` | 仅比对 hash，报告失效 / 缺失条目，不写入 | stale / skipped |

## 一致性

回填与 backend 写路径共用 `index_knowledge_content`，因此内容变更后无论经
写路径挂钩或本 CLI 补建，最终库中向量一致（同一 content_hash 幂等）。

## 测试

```bash
cd tools && ../.venv/Scripts/python -m pytest tests/test_backfill.py -q
```
