# app/services/vector_store.py
"""
向量数据库服务 - Qdrant集成
按 Qdrant 官方最佳实践：单 collection + payload 索引（category keyword + published_timestamp float）
"""
import hashlib
import re
import time
import uuid
from typing import List, Dict, Optional
from datetime import datetime
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance, VectorParams, PointStruct, Filter, FieldCondition,
    Range, MatchValue, MatchAny, PayloadSchemaType
)
from loguru import logger

from flask import current_app
from app.services.embedding_service import EmbeddingService
from app.utils.text_encoding import normalize_text, safe_for_display


# 用于去重的标点 / 空白正则
_DEDUP_STRIP_RE = re.compile(r'[\s，,、;；：:。.!！？?""\'\'「」【】《》\-—\u3000]+')
_HTML_TAG_RE = re.compile(r'<[^>]+>')


def make_dedup_key(item: Dict) -> str:
    """
    生成用于去重的标准化 key。
    去除 HTML 标签、标点、空白后的 title + source，解决近似重复。
    """
    title = item.get("title") or ""
    source = item.get("source") or ""
    title = _HTML_TAG_RE.sub("", title)
    title = _DEDUP_STRIP_RE.sub("", title)
    return title + source


class VectorStore:
    """向量数据库服务"""
    
    _instance = None
    _client = None
    
    def __new__(cls, embedding_service: EmbeddingService = None):
        """单例模式"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self, embedding_service: EmbeddingService = None):
        if self._client is not None:
            return
            
        self.embedding_service = embedding_service or EmbeddingService()
        
        # 从Flask配置获取参数
        try:
            qdrant_host = current_app.config.get('QDRANT_HOST', 'localhost')
            qdrant_port = current_app.config.get('QDRANT_PORT', 6333)
            self.collection_name = current_app.config.get('QDRANT_COLLECTION_NAME', 'news_collection')
        except RuntimeError:
            # 非Flask上下文时使用默认值
            qdrant_host = 'localhost'
            qdrant_port = 6333
            self.collection_name = 'news_collection'
        
        self._client = QdrantClient(
            host=qdrant_host,
            port=qdrant_port
        )
        self._ensure_collection()
    
    @property
    def client(self):
        return self._client
    
    def _ensure_collection(self):
        """确保集合存在，若不存在则创建并建 payload 索引"""
        try:
            collections = self.client.get_collections().collections
            collection_names = [c.name for c in collections]
            
            if self.collection_name not in collection_names:
                logger.info(f"创建向量集合: {self.collection_name}")
                self.client.create_collection(
                    collection_name=self.collection_name,
                    vectors_config=VectorParams(
                        size=self.embedding_service.get_embedding_dim(),
                        distance=Distance.COSINE
                    )
                )
                # 创建 payload 索引
                self._create_payload_indexes()
                logger.info("向量集合创建成功，payload 索引已建立")
            else:
                logger.info(f"向量集合已存在: {self.collection_name}")
        except Exception as e:
            logger.error(f"初始化向量集合失败: {e}")
            raise
    
    def _create_payload_indexes(self):
        """为 category、published_timestamp、source 创建 payload 索引"""
        try:
            self.client.create_payload_index(
                collection_name=self.collection_name,
                field_name="category",
                field_schema=PayloadSchemaType.KEYWORD,
            )
            logger.info("创建 payload 索引: category (keyword)")
        except Exception as e:
            logger.warning(f"创建 category 索引失败（可能已存在）: {e}")
        
        try:
            self.client.create_payload_index(
                collection_name=self.collection_name,
                field_name="published_timestamp",
                field_schema=PayloadSchemaType.FLOAT,
            )
            logger.info("创建 payload 索引: published_timestamp (float)")
        except Exception as e:
            logger.warning(f"创建 published_timestamp 索引失败（可能已存在）: {e}")
        
        try:
            self.client.create_payload_index(
                collection_name=self.collection_name,
                field_name="source",
                field_schema=PayloadSchemaType.KEYWORD,
            )
            logger.info("创建 payload 索引: source (keyword)")
        except Exception as e:
            logger.warning(f"创建 source 索引失败（可能已存在）: {e}")
    
    def search(
        self,
        query_text: str,
        top_k: int = 5,
        filter_source: Optional[str] = None,
        filter_category: Optional[str] = None,
        filter_date_from: Optional[str] = None,
        filter_date_to: Optional[str] = None
    ) -> List[Dict]:
        """
        向量搜索，支持 source、category、日期 range 过滤
        
        Args:
            query_text: 查询文本
            top_k: 返回top-k结果
            filter_source: 过滤来源
            filter_category: 过滤类别；非 general 时实际查询为 [该类别, general]，减少粗筛遗漏
            filter_date_from: 过滤起始日期（YYYY-MM-DD 或 ISO 格式）
            filter_date_to: 过滤结束日期（YYYY-MM-DD 或 ISO 格式）
            
        Returns:
            搜索结果列表
        """
        query_preview = query_text[:80] + "..." if len(query_text) > 80 else query_text
        logger.debug(
            f"[VectorStore.search] 入口: query_text={repr(query_preview)}, "
            f"top_k={top_k}, filter_category={filter_category}, filter_source={filter_source}"
        )
        t0 = time.perf_counter()
        query_vector = self.embedding_service.encode_query(query_text)
        
        # 构建过滤条件
        filter_conditions = []
        
        # source 过滤
        if filter_source:
            filter_conditions.append(
                FieldCondition(key="source", match=MatchValue(value=filter_source))
            )
        
        # category 过滤：非 general 时同时带上 general，避免粗筛遗漏
        if filter_category:
            if filter_category == "general":
                filter_conditions.append(
                    FieldCondition(key="category", match=MatchValue(value="general"))
                )
            else:
                filter_conditions.append(
                    FieldCondition(
                        key="category",
                        match=MatchAny(any=[filter_category, "general"])
                    )
                )
        
        # 日期 range 过滤（用 published_timestamp float）
        if filter_date_from:
            try:
                # 支持 YYYY-MM-DD 或 ISO 格式
                if "T" in filter_date_from:
                    dt = datetime.fromisoformat(filter_date_from)
                else:
                    dt = datetime.strptime(filter_date_from, "%Y-%m-%d")
                gte = dt.timestamp()
                filter_conditions.append(
                    FieldCondition(key="published_timestamp", range=Range(gte=gte))
                )
            except Exception as e:
                logger.warning(f"解析 filter_date_from 失败: {filter_date_from}, {e}")
        
        if filter_date_to:
            try:
                if "T" in filter_date_to:
                    dt = datetime.fromisoformat(filter_date_to)
                else:
                    # 结束日期默认到当天 23:59:59
                    dt = datetime.strptime(filter_date_to, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
                lte = dt.timestamp()
                filter_conditions.append(
                    FieldCondition(key="published_timestamp", range=Range(lte=lte))
                )
            except Exception as e:
                logger.warning(f"解析 filter_date_to 失败: {filter_date_to}, {e}")
        
        query_filter = Filter(must=filter_conditions) if filter_conditions else None
        
        # 执行搜索
        try:
            response = self.client.query_points(
                collection_name=self.collection_name,
                query=query_vector,
                limit=top_k,
                query_filter=query_filter
            )
            results = response.points or []
            
            # 格式化结果
            search_results = []
            seen_news_ids = set()
            
            for result in results:
                payload = result.payload or {}
                news_id = payload.get("news_id")
                
                # 去重：每个新闻只返回一次（取最相关的块）
                if news_id not in seen_news_ids:
                    seen_news_ids.add(news_id)
                    raw = lambda k: safe_for_display(normalize_text(payload.get(k)))
                    search_results.append({
                        "score": result.score,
                        "title": raw("title"),
                        "content": raw("content") or "",
                        "source": raw("source"),
                        "category": raw("category"),
                        "link": raw("link"),
                        "published_time": raw("published_time"),
                        "chunk_index": payload.get("chunk_index", 0)
                    })
            elapsed_ms = (time.perf_counter() - t0) * 1000
            results_preview = query_text[:40] + "..." if len(query_text) > 40 else query_text
            logger.info(
                f"[VectorStore.search] 完成: query_preview={repr(results_preview)}, "
                f"results={len(search_results)}, elapsed_ms={elapsed_ms:.2f}"
            )
            return search_results

        except Exception as e:
            logger.error(f"向量搜索失败: {e}")
            raise

    def search_with_expansion(
        self,
        queries: List[str],
        top_k: int = 5,
        filter_source: Optional[str] = None,
        filter_category: Optional[str] = None,
        filter_date_from: Optional[str] = None,
        filter_date_to: Optional[str] = None,
        fallback_query: Optional[str] = None,
        score_threshold: float = 0.3
    ) -> List[Dict]:
        """
        多查询扩展搜索：对多个查询变体分别检索，合并去重，空结果时 fallback。
        
        Args:
            queries: 查询变体列表（由 LLM expand_queries_for_search 生成）
            top_k: 最终返回 top-k 结果
            filter_source: 过滤来源
            filter_category: 过滤类别
            filter_date_from: 过滤起始日期
            filter_date_to: 过滤结束日期
            fallback_query: 空结果时的兜底查询（通常是更泛化的关键词）
            score_threshold: 结果质量阈值，低于此分数视为低质量
            
        Returns:
            合并去重后的搜索结果列表，按 score 降序
        """
        fallback_preview = (fallback_query[:40] + "..." if len(fallback_query or "") > 40 else fallback_query) if fallback_query else None
        logger.debug(
            f"[VectorStore.search_with_expansion] 入口: queries_count={len(queries)}, "
            f"top_k={top_k}, fallback_query={repr(fallback_preview)}"
        )
        all_results: Dict[str, Dict] = {}  # dedup_key -> result (keep highest score)

        # 1. 对每个查询变体执行搜索
        for query in queries:
            try:
                results = self.search(
                    query_text=query,
                    top_k=top_k,
                    filter_source=filter_source,
                    filter_category=filter_category,
                    filter_date_from=filter_date_from,
                    filter_date_to=filter_date_to
                )
                for item in results:
                    key = make_dedup_key(item)
                    if key not in all_results or item.get("score", 0) > all_results[key].get("score", 0):
                        all_results[key] = item
            except Exception as e:
                logger.warning(f"扩展查询 '{query}' 搜索失败: {e}")
                continue
        
        # 2. 合并结果按 score 降序排序
        merged = sorted(all_results.values(), key=lambda x: x.get("score", 0), reverse=True)
        
        # 3. 检查结果质量：如果为空或最高分低于阈值，尝试 fallback
        if fallback_query and (not merged or merged[0].get("score", 0) < score_threshold):
            logger.info(f"检索结果为空或质量低，尝试 fallback 查询: {fallback_query}")
            try:
                fallback_results = self.search(
                    query_text=fallback_query,
                    top_k=top_k,
                    filter_source=filter_source,
                    filter_category=filter_category,
                    filter_date_from=filter_date_from,
                    filter_date_to=filter_date_to
                )
                # 合并 fallback 结果
                for item in fallback_results:
                    key = make_dedup_key(item)
                    if key not in all_results or item.get("score", 0) > all_results[key].get("score", 0):
                        all_results[key] = item
                merged = sorted(all_results.values(), key=lambda x: x.get("score", 0), reverse=True)
            except Exception as e:
                logger.warning(f"Fallback 查询失败: {e}")
        
        logger.info(
            f"[VectorStore.search_with_expansion] 完成: queries_count={len(queries)}, "
            f"merged={len(merged)}, returned={min(len(merged), top_k)}"
        )
        return merged[:top_k]
    
    def get_collection_info(self) -> Dict:
        """获取集合信息"""
        try:
            info = self.client.get_collection(self.collection_name)
            return {
                "name": self.collection_name,
                "points_count": info.points_count,
                "vectors_count": getattr(info, "indexed_vectors_count", info.points_count),
            }
        except Exception as e:
            logger.error(f"获取集合信息失败: {e}")
            return {}
