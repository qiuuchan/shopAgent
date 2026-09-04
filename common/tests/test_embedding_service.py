# -*- coding: utf-8 -*-
"""
common.tests.test_embedding_service —— embedding 服务单元测试
=============================================================
本文件用途：以纯函数 + 打桩 ``urllib.request.urlopen`` 验证
``common.services.embedding_service``（POL-002）：
- 纯函数：配置校验 / 文本规整 / 向量 JSON 编解码往返 / 余弦相似度（零向量安全）；
- 网络函数 ``embed_texts``：成功 / 超时 / 非 200 / 返回结构缺字段 四分支；
- Hypothesis 属性测试：余弦对称性、自相似 = 1、JSON 编解码往返恒等；
- 配置回退：embedding api_base/api_key 缺省回退 chat 配置。

说明：网络打桩用 ``unittest.mock.patch`` 替换 ``urllib.request.urlopen``，
不发起真实网络请求；异常文本断言不含 API 密钥（保密，工单口径）。
"""
import json
import unittest.mock as mock
import urllib.error

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from common.services.embedding_service import (
    DEFAULT_EMBEDDING_MODEL,
    EmbeddingError,
    cosine_similarity,
    decode_vector,
    embed_texts,
    encode_vector,
    normalize_text,
    resolve_embedding_config,
    validate_embedding_config,
)


# ---------------------------------------------------------------------------
# 纯函数测试
# ---------------------------------------------------------------------------
def test_normalize_text_strips_and_removes_newlines() -> None:
    """normalize_text 去首尾空白与换行。"""
    assert normalize_text("  你好\n世界  ") == "你好 世界"
    assert normalize_text(None) == ""
    assert normalize_text("") == ""


def test_validate_embedding_config_missing_fields() -> None:
    """validate_embedding_config 返回缺失字段中文名。"""
    assert "API 密钥" in validate_embedding_config({"model": "text-embedding-v3"})
    assert "模型名称" in validate_embedding_config({"api_key": "k"})
    assert validate_embedding_config({"api_key": "k", "model": "m"}) == []
    assert len(validate_embedding_config(None)) == 2


def test_encode_decode_vector_roundtrip() -> None:
    """encode_vector / decode_vector 往返恒等。"""
    vector = [0.1, -0.2, 0.3]
    encoded = encode_vector(vector)
    assert isinstance(encoded, str)
    assert decode_vector(encoded) == pytest.approx(vector)


def test_decode_vector_invalid_returns_empty() -> None:
    """decode_vector 对非法值返回空列表。"""
    assert decode_vector(None) == []
    assert decode_vector("not-json") == []
    assert decode_vector("{}") == []  # 非列表
    assert decode_vector("[1, 'a']") == []  # 非数字列表


def test_cosine_similarity_basic() -> None:
    """余弦相似度：相同方向 = 1，正交 = 0。"""
    assert cosine_similarity([1, 0], [1, 0]) == pytest.approx(1.0)
    assert cosine_similarity([1, 0], [0, 1]) == pytest.approx(0.0)
    assert cosine_similarity([1, 2], [1, 2]) == pytest.approx(1.0)


def test_cosine_similarity_zero_vector_safe() -> None:
    """零向量 / 空向量 / 维度不一致安全返回 0.0。"""
    assert cosine_similarity([0, 0], [1, 1]) == 0.0
    assert cosine_similarity([], [1, 1]) == 0.0
    assert cosine_similarity([1], [1, 2]) == 0.0


def test_resolve_embedding_config_fallback_to_chat() -> None:
    """embedding api_base/api_key 缺省回退 chat 配置。"""
    resolved = resolve_embedding_config(
        chat_api_base="https://chat.example.com/v1",
        chat_api_key="chat-key",
        model="",
    )
    assert resolved["api_base"] == "https://chat.example.com/v1"
    assert resolved["api_key"] == "chat-key"
    assert resolved["model"] == DEFAULT_EMBEDDING_MODEL


def test_resolve_embedding_config_prefers_own() -> None:
    """embedding 有独立配置时优先自身，不回退。"""
    resolved = resolve_embedding_config(
        api_base="https://embed.example.com/v1",
        api_key="embed-key",
        model="my-model",
        chat_api_base="https://chat.example.com/v1",
        chat_api_key="chat-key",
    )
    assert resolved["api_base"] == "https://embed.example.com/v1"
    assert resolved["api_key"] == "embed-key"
    assert resolved["model"] == "my-model"


def test_resolve_embedding_config_defaults() -> None:
    """两者均缺省时用默认地址 + 默认模型。"""
    resolved = resolve_embedding_config()
    assert resolved["model"] == DEFAULT_EMBEDDING_MODEL


# ---------------------------------------------------------------------------
# 网络函数打桩测试（urllib.request.urlopen）
# ---------------------------------------------------------------------------
def _mock_response(payload: dict, status: int = 200) -> mock.MagicMock:
    """构造 urlopen 返回值：一个支持上下文管理的伪响应对象。"""
    resp = mock.MagicMock()
    resp.status = status
    resp.read.return_value = json.dumps(payload).encode("utf-8")
    resp.__enter__ = mock.Mock(return_value=resp)
    resp.__exit__ = mock.Mock(return_value=False)
    return resp


def test_embed_texts_success() -> None:
    """正常返回：按 index 恢复顺序并取 embedding。"""
    payload = {
        "data": [
            {"embedding": [0.3, 0.4], "index": 1},
            {"embedding": [0.1, 0.2], "index": 0},
        ]
    }
    with mock.patch(
        "urllib.request.urlopen", return_value=_mock_response(payload)
    ) as mocked:
        vectors = embed_texts(
            ["文本A", "文本B"], api_key="k", api_base="https://x.com/v1", model="m"
        )
    assert vectors == [[0.1, 0.2], [0.3, 0.4]]
    # 请求体含 model 与 input
    call_payload = json.loads(mocked.call_args[0][0].data)
    assert call_payload["model"] == "m"
    assert call_payload["input"] == ["文本A", "文本B"]


def test_embed_texts_non_200_raises() -> None:
    """非 200：抛出 EmbeddingError 且异常文本不含密钥。"""
    http_err = urllib.error.HTTPError("url", 401, "Unauthorized", {}, mock.MagicMock())
    http_err.read = mock.Mock(
        return_value=json.dumps({"error": {"message": "invalid key"}}).encode("utf-8")
    )
    with mock.patch("urllib.request.urlopen", side_effect=http_err) as mocked:
        with pytest.raises(EmbeddingError) as exc:
            embed_texts(
                ["文本"], api_key="SECRET-KEY", api_base="https://x.com/v1", model="m"
            )
    assert "SECRET-KEY" not in str(exc.value)
    assert "401" in str(exc.value)


def test_embed_texts_timeout_raises() -> None:
    """连接超时 / URLError：抛出 EmbeddingError 且不含密钥。"""
    with mock.patch(
        "urllib.request.urlopen", side_effect=urllib.error.URLError("timeout")
    ):
        with pytest.raises(EmbeddingError) as exc:
            embed_texts(
                ["文本"], api_key="SECRET-KEY", api_base="https://x.com/v1", model="m"
            )
    assert "SECRET-KEY" not in str(exc.value)


def test_embed_texts_missing_field_raises() -> None:
    """返回结构缺 data / 条目缺 embedding：抛出 EmbeddingError。"""
    # 缺 data
    with mock.patch("urllib.request.urlopen", return_value=_mock_response({ })):
        with pytest.raises(EmbeddingError):
            embed_texts(["文本"], api_key="k", api_base="https://x.com/v1", model="m")
    # 条目缺 embedding
    with mock.patch(
        "urllib.request.urlopen",
        return_value=_mock_response({"data": [{"index": 0}]}),
    ):
        with pytest.raises(EmbeddingError):
            embed_texts(["文本"], api_key="k", api_base="https://x.com/v1", model="m")


def test_embed_texts_missing_key_raises() -> None:
    """密钥缺失：直接抛出 EmbeddingError，不触网络。"""
    with pytest.raises(EmbeddingError):
        embed_texts(["文本"], api_key="", api_base="https://x.com/v1", model="m")


def test_embed_texts_empty_inputs_returns_empty() -> None:
    """空文本列表：返回空向量列表。"""
    assert embed_texts([], api_key="k", api_base="https://x.com/v1", model="m") == []


# ---------------------------------------------------------------------------
# Hypothesis 属性测试
# ---------------------------------------------------------------------------
@settings(max_examples=100, deadline=None)
@given(a=st.lists(st.floats(min_value=-1, max_value=1), min_size=1, max_size=20))
def test_cosine_symmetry(a) -> None:
    """余弦相似度对称：cos(a, b) == cos(b, a)。"""
    b = [x * 2.0 for x in a]
    assert cosine_similarity(a, b) == pytest.approx(cosine_similarity(b, a))


@settings(max_examples=100, deadline=None)
@given(v=st.lists(st.floats(min_value=-1, max_value=1), min_size=1, max_size=20))
def test_cosine_self_similarity_is_one(v) -> None:
    """自相似 = 1（非零向量）。"""
    if any(x != 0 for x in v):
        assert cosine_similarity(v, v) == pytest.approx(1.0)


@settings(max_examples=100, deadline=None)
@given(
    v=st.lists(
        st.floats(min_value=-100, max_value=100, allow_nan=False, allow_infinity=False),
        min_size=0,
        max_size=50,
    )
)
def test_encode_decode_roundtrip_identity(v) -> None:
    """JSON 编解码往返恒等（含空列表）。"""
    assert decode_vector(encode_vector(v)) == pytest.approx(v, abs=1e-9)
