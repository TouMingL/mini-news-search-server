# app/services/vector_store.py
"""
向量数据库服务 - Qdrant 集成
支持单向量（dense）与混合检索（dense + sparse，RRF 融合）。混合检索需 collection 含 sparse 向量且启用 RETRIEVAL_HYBRID_ENABLED。
"""
import hashlib
import re
import time
import uuid
from typing import List, Dict, Optional, Any
from datetime import datetime
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance, VectorParams, PointStruct, Filter, FieldCondition,
    Range, MatchValue, MatchAny, PayloadSchemaType,
    SparseVector, SparseVectorParams, SparseVectorsConfig,
    Prefetch, FusionQuery, Fusion,
)
from loguru import logger

from flask import current_app
from app.services.embedding_service import EmbeddingService
from app.utils.text_encoding import normalize_text, safe_for_display


# 用于去重的标点 / 空白正则
_DEDUP_STRIP_RE = re.compile(r'[\s，,、;；：:。.!！？?""\'\'「」【】《》\-—\u3000]+')
_HTML_TAG_RE = re.compile(r'<[^>]+>')


def _parent_source(source: str) -> str:
    """
    提取大源：source 形如 "金十-黄金" 时返回 "金十"，
    无 "-" 时返回原 source（如 "新浪财经"）。
    """
    s = (source or "").strip()
    if "-" in s:
        return s.split("-", 1)[0]
    return s


def make_dedup_key(item: Dict) -> str:
    """
    生成用于去重的标准化 key。
    同大源、同标题视为同一结果（不同细分如 金十-黄金/金十-钯金 合并）。
    去除 HTML 标签、标点、空白后的 title + 大源。
    """
    title = item.get("title") or ""
    source = item.get("source") or ""
    title = _HTML_TAG_RE.sub("", title)
    title = _DEDUP_STRIP_RE.sub("", title)
    parent = _parent_source(source)
    return title + parent


def _get_hybrid_config() -> Dict[str, Any]:
    try:
        c = current_app.config
        return {
            "enabled": c.get("RETRIEVAL_HYBRID_ENABLED", False),
            "dense_name": (c.get("RETRIEVAL_DENSE_VECTOR_NAME") or "").strip() or None,
            "sparse_name": (c.get("RETRIEVAL_SPARSE_VECTOR_NAME") or "sparse").strip(),
        }
    except Exception:
        return {"enabled": False, "dense_name": None, "sparse_name": "sparse"}


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
        self._last_retrieval_mode: Optional[str] = None
        self._ensure_collection()
    
    @property
    def client(self):
        return self._client
    
    def _ensure_collection(self):
        """确保集合存在；若不存在则创建。启用混合检索时新集合将包含 dense + sparse 向量配置。"""
        try:
            collections = self.client.get_collections().collections
            collection_names = [c.name for c in collections]
            hybrid = _get_hybrid_config()

            if self.collection_name not in collection_names:
                logger.info(f"创建向量集合: {self.collection_name}")
                dim = self.embedding_service.get_embedding_dim()
                if hybrid["enabled"] and hybrid["dense_name"]:
                    vectors_config = {
                        hybrid["dense_name"]: VectorParams(
                            size=dim,
                            distance=Distance.COSINE,
                        )
                    }
                    sparse_config = {
                        hybrid["sparse_name"]: SparseVectorParams(),
                    }
                    self.client.create_collection(
                        collection_name=self.collection_name,
                        vectors_config=vectors_config,
                        sparse_vectors_config=sparse_config,
                    )
                    logger.info("向量集合创建成功（含 sparse，支持混合检索）")
                else:
                    self.client.create_collection(
                        collection_name=self.collection_name,
                        vectors_config=VectorParams(
                            size=dim,
                            distance=Distance.COSINE,
                        )
                    )
                    logger.info("向量集合创建成功")
                self._create_payload_indexes()
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
        filter_categories: Optional[List[str]] = None,
        filter_date_from: Optional[str] = None,
        filter_date_to: Optional[str] = None,
        filter_event_time_from: Optional[str] = None,
        filter_event_time_to: Optional[str] = None,
    ) -> List[Dict]:
        """
        向量搜索，支持 source、category、日期 range 过滤。

        category 优先用 filter_categories（top-k 列表，在前若干类中搜），
        否则用 filter_category（单类 + general）。
        """
        query_preview = query_text[:80] + "..." if len(query_text) > 80 else query_text
        logger.debug(
            f"[VectorStore.search] 入口: query_text={repr(query_preview)}, "
            f"top_k={top_k}, filter_categories={filter_categories}, filter_category={filter_category}, filter_source={filter_source}"
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
        
        # category 过滤：优先 filter_categories（top-k 类别），并始终加入 general（入库时每类都有部分落在 general）
        if filter_categories:
            cats = list(filter_categories)
            if "general" not in cats:
                cats = cats + ["general"]
            filter_conditions.append(
                FieldCondition(key="category", match=MatchAny(any=cats))
            )
        elif filter_category:
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

        # event_time 范围过滤（用 event_time_timestamp float，有则过滤）
        if filter_event_time_from:
            try:
                if "T" in filter_event_time_from:
                    dt = datetime.fromisoformat(filter_event_time_from)
                else:
                    dt = datetime.strptime(filter_event_time_from, "%Y-%m-%d")
                gte = dt.timestamp()
                filter_conditions.append(
                    FieldCondition(key="event_time_timestamp", range=Range(gte=gte))
                )
            except Exception as e:
                logger.warning(f"解析 filter_event_time_from 失败: {filter_event_time_from}, {e}")
        if filter_event_time_to:
            try:
                if "T" in filter_event_time_to:
                    dt = datetime.fromisoformat(filter_event_time_to)
                else:
                    dt = datetime.strptime(filter_event_time_to, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
                lte = dt.timestamp()
                filter_conditions.append(
                    FieldCondition(key="event_time_timestamp", range=Range(lte=lte))
                )
            except Exception as e:
                logger.warning(f"解析 filter_event_time_to 失败: {filter_event_time_to}, {e}")
        
        query_filter = Filter(must=filter_conditions) if filter_conditions else None
        
        # 过采：多拉候选，内存中两级去重后返回 top_k
        fetch_limit = top_k * 5

        # 混合检索：若启用且 collection 含 sparse 且能拿到 query sparse，则用 Prefetch + RRF
        hybrid_cfg = _get_hybrid_config()
        use_hybrid = False
        hybrid_reason = ""
        if hybrid_cfg["enabled"] and hybrid_cfg["dense_name"]:
            try:
                coll = self.client.get_collection(self.collection_name)
                sparse_params = getattr(getattr(coll, "config", None), "params", None)
                has_sparse = sparse_params and getattr(sparse_params, "sparse_vectors", None)
                query_sparse = self.embedding_service.encode_query_sparse(query_text)
                if has_sparse and query_sparse:
                    use_hybrid = True
                elif not has_sparse:
                    hybrid_reason = "collection无sparse向量"
                else:
                    hybrid_reason = "embed_sparse不可用或返回空"
            except Exception as e:
                logger.debug(f"[VectorStore.search] 混合检索检查失败，回退纯 dense: {e}")
                hybrid_reason = str(e)
        else:
            if not hybrid_cfg["enabled"]:
                hybrid_reason = "hybrid未启用"
            else:
                hybrid_reason = "未配置dense向量名"
        if use_hybrid:
            logger.info("[VectorStore.search] 检索模式: dense+sparse (RRF 融合)")
            self._last_retrieval_mode = "dense+sparse"
        else:
            logger.info("[VectorStore.search] 检索模式: dense-only" + (f"，原因: {hybrid_reason}" if hybrid_reason else ""))
            self._last_retrieval_mode = "dense-only"

        try:
            if use_hybrid:
                indices, values = query_sparse
                sparse_vec = SparseVector(indices=indices, values=values)
                response = self.client.query_points(
                    collection_name=self.collection_name,
                    prefetch=[
                        Prefetch(
                            query=query_vector,
                            using=hybrid_cfg["dense_name"],
                            limit=fetch_limit,
                        ),
                        Prefetch(
                            query=sparse_vec,
                            using=hybrid_cfg["sparse_name"],
                            limit=fetch_limit,
                        ),
                    ],
                    query=FusionQuery(fusion=Fusion.RRF),
                    limit=fetch_limit,
                    query_filter=query_filter,
                )
                results = response.points or []
                logger.info("[VectorStore.search] dense+sparse RRF 返回 %d 条候选", len(results))
            else:
                response = self.client.query_points(
                    collection_name=self.collection_name,
                    query=query_vector,
                    limit=fetch_limit,
                    query_filter=query_filter
                )
                results = response.points or []
            
            # 两级去重（同 news_id 只保留最相关 chunk；同大源+同标题只保留一条）
            search_results = []
            seen_news_ids = set()
            seen_dedup_keys = set()
            
            for result in results:
                payload = result.payload or {}
                news_id = payload.get("news_id")
                
                # 第一级：chunk 去重（同 news_id 只保留最相关 chunk）
                if news_id in seen_news_ids:
                    continue
                seen_news_ids.add(news_id)

                raw = lambda k: safe_for_display(normalize_text(payload.get(k)))
                item = {
                    "score": result.score,
                    "title": raw("title"),
                    "content": raw("content") or "",
                    "source": raw("source"),
                    "category": raw("category"),
                    "link": raw("link"),
                    "published_time": raw("published_time"),
                    "chunk_index": payload.get("chunk_index", 0),
                }
                if payload.get("rule_event_time") is not None:
                    item["rule_event_time"] = raw("rule_event_time")
                if payload.get("event_time_timestamp") is not None:
                    item["event_time_timestamp"] = payload.get("event_time_timestamp")
                if payload.get("event_time_confidence") is not None:
                    item["event_time_confidence"] = payload.get("event_time_confidence")
                if payload.get("event_time_source") is not None:
                    item["event_time_source"] = raw("event_time_source")

                # 第二级：稿件去重（同大源 + 同标题 → 同一篇稿件）
                dedup_key = make_dedup_key(item)
                if dedup_key in seen_dedup_keys:
                    continue
                seen_dedup_keys.add(dedup_key)

                search_results.append(item)
                if len(search_results) >= top_k:
                    break

            elapsed_ms = (time.perf_counter() - t0) * 1000
            results_preview = query_text[:40] + "..." if len(query_text) > 40 else query_text
            mode = "dense+sparse" if use_hybrid else "dense-only"
            logger.info(
                f"[VectorStore.search] 完成: mode={mode}, query_preview={repr(results_preview)}, "
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
        filter_categories: Optional[List[str]] = None,
        filter_date_from: Optional[str] = None,
        filter_date_to: Optional[str] = None,
        filter_event_time_from: Optional[str] = None,
        filter_event_time_to: Optional[str] = None,
        fallback_query: Optional[str] = None,
        score_threshold: float = 0.3
    ) -> List[Dict]:
        """
        多查询扩展搜索：对多个查询变体分别检索，合并去重，空结果时 fallback。
        category 优先 filter_categories（top-k 列表），否则 filter_category。
        """
        fallback_preview = (fallback_query[:40] + "..." if len(fallback_query or "") > 40 else fallback_query) if fallback_query else None
        logger.debug(
            f"[VectorStore.search_with_expansion] 入口: queries_count={len(queries)}, "
            f"top_k={top_k}, fallback_query={repr(fallback_preview)}"
        )
        all_results: Dict[str, Dict] = {}  # dedup_key -> result (keep highest score)

        # 1. 对每个查询变体执行搜索（search() 内部已做过采+两级去重，此处正常传 top_k）
        for query in queries:
            try:
                results = self.search(
                    query_text=query,
                    top_k=top_k,
                    filter_source=filter_source,
                    filter_category=filter_category,
                    filter_categories=filter_categories,
                    filter_date_from=filter_date_from,
                    filter_date_to=filter_date_to,
                    filter_event_time_from=filter_event_time_from,
                    filter_event_time_to=filter_event_time_to,
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
                    filter_categories=filter_categories,
                    filter_date_from=filter_date_from,
                    filter_date_to=filter_date_to,
                    filter_event_time_from=filter_event_time_from,
                    filter_event_time_to=filter_event_time_to,
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
