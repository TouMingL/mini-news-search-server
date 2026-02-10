# app/services/embedding_service.py
"""
Embedding 服务 - 通过 HTTP 调用独立 Embedding 进程，主进程不再加载模型。
独立服务启动: python embedding_server.py（默认 0.0.0.0:8083）
配置: EMBEDDING_SERVICE_URL（如 http://127.0.0.1:8083）
"""
import time
from typing import List, Optional, Union

import requests
from loguru import logger

from flask import current_app


class EmbeddingService:
    """Embedding 服务客户端：调用独立 embedding_server，不加载模型。"""

    _instance = None
    _dim_cache: Optional[int] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        self._base_url = None
        self._timeout = 30

    def _get_base_url(self) -> str:
        if self._base_url is not None:
            return self._base_url
        try:
            url = current_app.config.get('EMBEDDING_SERVICE_URL')
            timeout = current_app.config.get('EMBEDDING_SERVICE_TIMEOUT', 30)
        except RuntimeError:
            url = None
            timeout = 30
        if not url or not str(url).strip():
            raise RuntimeError(
                '未配置 EMBEDDING_SERVICE_URL。请先启动独立 Embedding 服务: python embedding_server.py，'
                '并在 .env 中设置 EMBEDDING_SERVICE_URL=http://127.0.0.1:8083'
            )
        self._base_url = url.rstrip('/')
        self._timeout = int(timeout)
        return self._base_url

    @property
    def model(self):
        """兼容旧代码中可能访问的 model 属性；客户端无本地 model，返回 None。"""
        return None

    def encode(
        self,
        texts: Union[str, List[str]],
        normalize_embeddings: bool = True,
        prompt_name: Optional[str] = None,
    ) -> Union[List[float], List[List[float]]]:
        """通过 HTTP 调用独立服务生成文本向量。"""
        if isinstance(texts, str):
            texts = [texts]
        n = len(texts)
        preview = (texts[0][:50] + "..." if len(texts[0]) > 50 else texts[0]) if texts else ""
        logger.debug(
            f"[Embedding] encode 调用(HTTP): texts_count={n}, prompt_name={prompt_name}, preview={repr(preview)}"
        )
        base = self._get_base_url()
        t0 = time.perf_counter()
        r = requests.post(
            f"{base}/embed",
            json={
                "texts": texts,
                "normalize_embeddings": normalize_embeddings,
                "prompt_name": prompt_name,
            },
            timeout=self._timeout,
        )
        r.raise_for_status()
        data = r.json()
        elapsed_ms = (time.perf_counter() - t0) * 1000
        logger.info(
            f"[Embedding] encode 完成(HTTP): texts_count={n}, elapsed_ms={elapsed_ms:.2f}"
        )
        out = data.get("embeddings")
        if out is None:
            raise RuntimeError("Embedding 服务返回格式错误: 缺少 embeddings")
        if len(texts) == 1:
            return out[0] if isinstance(out[0], list) else out
        return out

    def encode_query(self, query_text: str, normalize_embeddings: bool = True) -> List[float]:
        """对检索查询编码；请求中带 prompt_name=query。"""
        return self.encode(
            query_text,
            normalize_embeddings=normalize_embeddings,
            prompt_name="query",
        )

    def get_embedding_dim(self) -> int:
        """获取向量维度（结果会缓存）。"""
        if self._dim_cache is not None:
            return self._dim_cache
        base = self._get_base_url()
        r = requests.get(f"{base}/embedding_dim", timeout=self._timeout)
        r.raise_for_status()
        data = r.json()
        dim = data.get("dim")
        if dim is None:
            raise RuntimeError("Embedding 服务返回格式错误: 缺少 dim")
        self._dim_cache = int(dim)
        return self._dim_cache
