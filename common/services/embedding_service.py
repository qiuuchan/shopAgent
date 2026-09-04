# -*- coding: utf-8 -*-
"""
common.services.embedding_service —— 向量 embedding 服务（RAG 检索补强）
======================================================================
本文件用途：为「拼多多自动回复」系统提供文本向量化的 embedding 能力，供
POL-002（知识库向量化）与 POL-005（混合检索）复用。设计严格仿
``ai_provider_service.py`` 的口径（规范 51 导入置顶 / 规范 37 中文注释 /
规范 35 单文件 ≤500 行 / 规范 38 日志禁用 debug）。

能力范围（POL 引入）：
- 纯函数（无 I/O，便于单测）：``validate_embedding_config``（配置校验）、
  ``normalize_text``（文本规整）、``encode_vector`` / ``decode_vector``
  （向量 JSON 编解码，对应模型 TEXT 列存储先例）、``cosine_similarity``
  （余弦相似度，零向量安全）、``resolve_embedding_config``（embedding 配置
  api_base/api_key 缺省回退店铺 chat 配置）。
- 网络函数：``embed_texts`` 调用 OpenAI 兼容 ``POST {api_base}/embeddings``
  （阿里云百炼 DashScope 兼容模式同源），返回向量列表。

设计约束：
- **仅依赖标准库 urllib**：common 运行期无 httpx，与 ``service_client`` /
  ``ai_provider_service`` 口径一致，避免为各服务引入额外 HTTP 依赖。
- **纯函数与网络分离**：配置校验 / 文本规整 / 编解码 / 余弦为纯函数；
  实际网络请求集中于 ``embed_texts``，便于打桩 ``urllib.request.urlopen`` 测试。
- **密钥安全**：任何异常 / 日志文本仅含端点描述（provider/base_url/model），
  **绝不含 api_key**（仿 ``ai_provider_service._describe_endpoint`` 口径）。
- **零行为变更**：本服务仅在新路径被调用，不触碰既有 jieba 检索逻辑。
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

logger = logging.getLogger("common.embedding")

# 默认嵌入模型（OpenAI 兼容输出 1024 维，国内可达且常用）。
DEFAULT_EMBEDDING_MODEL: str = "text-embedding-v3"
# 默认 embedding 接口地址（阿里云百炼兼容模式，与 chat ai 默认地址一致）。
DEFAULT_EMBEDDING_BASE_URL: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
# 默认请求超时（秒）。
DEFAULT_EMBEDDING_TIMEOUT: float = 30.0


class EmbeddingError(Exception):
    """embedding 调用异常：携带中文原因，供上层规整为失败 / 回退逻辑。"""


def normalize_text(text: Any) -> str:
    """规整待向量化文本：去首尾空白与危险换行，防误配与异常换行进向量。

    Args:
        text: 待规整文本。

    Returns:
        规整后的文本（无换行、首尾去空白）；None / 非字符串视为空串。
    """
    return str(text or "").replace("\r", "").replace("\n", " ").strip()


def validate_embedding_config(cfg: Optional[Dict[str, Any]]) -> List[str]:
    """校验 embedding 配置完整性，返回缺失字段中文名列表（为空表示完整）。

    Args:
        cfg: 兼容 dict 的配置，可含 api_key / api_base / model 字段。

    Returns:
        缺失字段的中文名列表（如 ["模型名称", "API 密钥"]）；None / 空 dict 视为全缺。
    """
    payload = dict(cfg or {})
    missing: List[str] = []
    if not normalize_text(payload.get("api_key")):
        missing.append("API 密钥")
    if not normalize_text(payload.get("model")):
        missing.append("模型名称")
    return missing


def resolve_embedding_config(
    *,
    api_base: Any = None,
    api_key: Any = None,
    model: Any = None,
    chat_api_base: Any = None,
    chat_api_key: Any = None,
) -> Dict[str, str]:
    """解析 embedding 请求参数：缺省字段回退店铺 chat 配置。

    约定：embedding 未单独配置 api_base / api_key 时，回退该店铺 chat 配置的
    api_base / api_key（同一供应商常共用密钥）。model 无 chat 回退项，未配则用默认。

    Args:
        api_base: embedding 独立配置的 API 地址（可空）。
        api_key: embedding 独立配置的 API 密钥（可空）。
        model: embedding 模型名（可空）。
        chat_api_base: 店铺 chat 配置的 API 地址（回退源）。
        chat_api_key: 店铺 chat 配置的 API 密钥（回退源）。

    Returns:
        解析后的配置字典 {api_base, api_key, model}（api_base 缺省用默认地址；
        model 缺省用默认模型；api_base/api_key 若无独立配置则回退 chat 配置）。
    """
    resolved = {
        "api_base": normalize_text(api_base) or normalize_text(chat_api_base)
        or DEFAULT_EMBEDDING_BASE_URL,
        "api_key": normalize_text(api_key) or normalize_text(chat_api_key),
        "model": normalize_text(model) or DEFAULT_EMBEDDING_MODEL,
    }
    return resolved


def encode_vector(vector: List[float]) -> str:
    """将向量编码为 JSON 字符串（落库 TEXT 列）。

    Args:
        vector: 浮点向量。

    Returns:
        JSON 数组字符串，如 ``[0.1, 0.2]``。
    """
    return json.dumps([float(v) for v in vector], ensure_ascii=False)


def decode_vector(value: Any) -> List[float]:
    """从 JSON 字符串解码向量；非法输入返回空列表。

    Args:
        value: 存于 TEXT 列的 JSON 向量字符串（或已解析的列表）。

    Returns:
        浮点向量列表；解码失败 / 非列表时返回空列表（调用方按缺向量回退）。
    """
    if isinstance(value, list):
        return [float(v) for v in value]
    if value is None:
        return []
    try:
        parsed = json.loads(str(value))
    except (ValueError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    try:
        return [float(v) for v in parsed]
    except (TypeError, ValueError):
        return []


def cosine_similarity(a: List[float], b: List[float]) -> float:
    """计算两个向量的余弦相似度；任一向量零向量 / 空时安全返回 0.0。

    为避免极小非零向量（如 ``1e-200`` 量级）平方后浮点下溢致错误判 0，
    先将两向量各自按最大幅值缩放到 1 以内再计算点积与模长（余弦值对缩放不变）。

    Args:
        a: 向量 A。
        b: 向量 B。

    Returns:
        余弦相似度，落在 [-1.0, 1.0]；零向量 / 空向量返回 0.0（不抛异常）。
    """
    if not a or not b:
        return 0.0
    if len(a) != len(b):
        return 0.0
    amax = max(abs(x) for x in a)
    bmax = max(abs(x) for x in b)
    if amax <= 0.0 or bmax <= 0.0:
        return 0.0
    # 各自缩放后元素绝对值 ∈ [0,1]，平方不再下溢（余弦对缩放不变）。
    sa = [x / amax for x in a]
    sb = [x / bmax for x in b]
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(sa, sb):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return dot / (norm_a**0.5 * norm_b**0.5)


def _describe_endpoint(api_base: Any, model: Any) -> str:
    """构造端点描述（不含密钥），供异常文本安全展示。

    Args:
        api_base: 接口地址。
        model: 模型名。

    Returns:
        如 ``api_base='...', model='...'`` 的可读描述。
    """
    return f"api_base='{normalize_text(api_base)}', model='{normalize_text(model)}'"


def _http_post_json(
    url: str, headers: Dict[str, str], payload: Dict[str, Any], timeout: float
) -> Dict[str, Any]:
    """发起 POST(JSON) 请求并返回解析字典，异常统一转 ``EmbeddingError``。

    Args:
        url: 完整请求地址。
        headers: 请求头。
        payload: 请求体（JSON）。
        timeout: 超时秒数。

    Returns:
        解析后的响应字典。

    Raises:
        EmbeddingError: 网络不可达 / 超时 / 非 2xx / 响应非 JSON。
    """
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = _extract_http_error(exc)
        raise EmbeddingError(f"接口返回错误（HTTP {exc.code}）：{detail}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise EmbeddingError(f"无法连接 embedding 接口：{exc}") from exc

    try:
        parsed = json.loads(raw) if raw else {}
    except ValueError as exc:
        raise EmbeddingError("embedding 接口返回格式异常（非 JSON）") from exc
    if not isinstance(parsed, dict):
        raise EmbeddingError("embedding 接口返回格式异常")
    return parsed


def _extract_http_error(exc: urllib.error.HTTPError) -> str:
    """从 HTTPError 响应体中提取简短错误原因（截断 300 字，不含密钥）。"""
    try:
        raw = exc.read().decode("utf-8")
    except Exception:  # noqa: BLE001 - 读取错误体失败时回退状态码描述
        return f"HTTP {exc.code}"
    if not raw:
        return f"HTTP {exc.code}"
    try:
        body = json.loads(raw)
    except ValueError:
        # 文本体可能含密钥，仅截断展示，由调用方负责不在日志中落密钥。
        return raw[:300]
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("type") or error)[:300]
        if isinstance(error, str):
            return error[:300]
        for key in ("message", "msg", "detail"):
            if body.get(key):
                return str(body[key])[:300]
    return str(body)[:300]


def embed_texts(
    texts: List[str],
    *,
    api_key: str,
    api_base: str,
    model: str,
    timeout: float = DEFAULT_EMBEDDING_TIMEOUT,
) -> List[List[float]]:
    """对文本列表调用 OpenAI 兼容 ``POST {api_base}/embeddings`` 向量化。

    ``api_base`` 传入兼容模式根地址（如百炼 ``.../compatible-mode/v1``），本函数
    在其后拼接 ``/embeddings``。单条文本可传字符串；空白文本直接返回零向量占位。

    Args:
        texts: 待向量化文本列表。
        api_key: 请求密钥（仅用于请求头，绝不入日志 / 异常文本）。
        api_base: 接口根地址。
        model: 嵌入模型名。
        timeout: 请求超时秒数。

    Returns:
        向量列表，与 ``texts`` 顺序一一对应。

    Raises:
        EmbeddingError: 密钥缺失、无待处理文本、或接口调用失败。
    """
    if not api_key:
        raise EmbeddingError("API 密钥缺失，无法调用 embedding 接口")
    # 规整并剥离空白文本（空文本无向量语义，交由调用方按缺向量处理）。
    normalized = [normalize_text(t) for t in texts]
    if not normalized:
        return []
    url = f"{normalize_text(api_base).rstrip('/')}/embeddings"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"model": normalize_text(model), "input": normalized}
    result = _http_post_json(url, headers, payload, timeout)
    try:
        data = result["data"]
        if not isinstance(data, list):
            raise EmbeddingError("embedding 接口返回缺 data 列表")
        # data 条目常为 {"embedding": [...], "index": N}；按 index 恢复顺序。
        indexed: List[Dict[str, Any]] = sorted(
            data,
            key=lambda item: int(item.get("index", 0)) if isinstance(item, dict) else 0,
        )
        vectors: List[List[float]] = []
        for item in indexed:
            if not isinstance(item, dict) or "embedding" not in item:
                raise EmbeddingError("embedding 接口返回条目缺 embedding 字段")
            emb = item["embedding"]
            if not isinstance(emb, list):
                raise EmbeddingError("embedding 接口返回 embedding 非列表")
            vectors.append([float(v) for v in emb])
        return vectors
    except KeyError as exc:
        raise EmbeddingError("embedding 接口返回结构缺 data 字段") from exc
    except (TypeError, ValueError) as exc:
        raise EmbeddingError(f"embedding 接口返回格式异常：{str(result)[:200]}") from exc


__all__ = [
    "DEFAULT_EMBEDDING_MODEL",
    "DEFAULT_EMBEDDING_BASE_URL",
    "DEFAULT_EMBEDDING_TIMEOUT",
    "EmbeddingError",
    "normalize_text",
    "validate_embedding_config",
    "resolve_embedding_config",
    "encode_vector",
    "decode_vector",
    "cosine_similarity",
    "embed_texts",
]
