# app/services/rag_service.py
"""
RAG服务 - 整合检索与生成；Pipeline 为推荐路径，支持多轮与流式。
"""
import time
from typing import List, Dict, Optional, Iterator
from loguru import logger

from app.services.vector_store import VectorStore
from app.services.llm_service import LLMService
from app.services.answer_verifier import get_replacement_message


class RAGService:
    """
    RAG服务，整合向量检索和LLM生成
    
    支持两种模式：
    1. 传统模式：直接调用 query() 方法
    2. Pipeline模式：调用 query_with_pipeline() 方法，支持多轮对话
    """
    
    def __init__(
        self,
        vector_store: VectorStore = None,
        llm_service: LLMService = None,
        pipeline = None
    ):
        self.vector_store = vector_store or VectorStore()
        self.llm_service  = llm_service or LLMService()
        self._pipeline    = pipeline
    
    @property
    def pipeline(self):
        """延迟初始化 Pipeline"""
        if self._pipeline is None:
            from app.services.pipeline import get_pipeline
            self._pipeline = get_pipeline()
        return self._pipeline
    
    def query(
        self,
        query: str,
        top_k: int = 5,
        filter_source:    Optional[str] = None,
        filter_category:  Optional[str] = None,
        filter_date_from: Optional[str] = None,
        filter_date_to:   Optional[str] = None
    ) -> Dict:
        """
        执行RAG查询（传统模式，保持向后兼容）
        
        Args:
            query: 查询问题
            top_k: 返回top-k结果
            filter_source: 过滤来源
            filter_category: 过滤类别
            filter_date_from: 过滤起始日期
            filter_date_to: 过滤结束日期
            
        Returns:
            查询响应，包含answer、sources、query_time
        """
        start_time = time.time()
        
        try:
            # 1. 查询改写：将用户问题改为向量检索友好的关键词/短句
            search_query = self.llm_service.rewrite_query_for_search(query)
            logger.info(f"检索用查询(改写后): {search_query}")
            if search_query != query:
                logger.info(f"原始问题: {query}")

            # 2. 向量检索（使用改写后的查询，支持 source/category/日期过滤）
            search_results = self.vector_store.search(
                query_text=search_query,
                top_k=top_k,
                filter_source=filter_source,
                filter_category=filter_category,
                filter_date_from=filter_date_from,
                filter_date_to=filter_date_to
            )

            logger.info(f"RAG 搜索结果: 共 {len(search_results)} 条")
            for i, item in enumerate(search_results, 1):
                logger.info(
                    f"  [{i}] score={item.get('score', 0):.4f} | {item.get('published_time', '')} | {item.get('source', '')} | {item.get('title', '')[:60]}"
                )

            if not search_results:
                return {
                    "answer": "抱歉，没有找到相关的新闻内容。",
                    "sources": [],
                    "query_time": time.time() - start_time
                }

            # 3. LLM生成回答（仅生成）
            logger.info("调用LLM生成回答")
            answer = self.llm_service.generate_answer(
                query=query,
                context=search_results
            )
            result = self.pipeline.answer_verifier.verify(
                query=query,
                answer=answer,
                context=search_results,
            )
            if not result.passed:
                answer = get_replacement_message(result.failure_reason)
            else:
                answer = self.llm_service.post_process_answer(answer, search_results)
            
            # 4. 格式化来源信息（含 category）
            sources = [
                {
                    "title": item["title"],
                    "source": item["source"],
                    "category": item.get("category"),
                    "link": item["link"],
                    "score": item["score"],
                    "published_time": item.get("published_time")
                }
                for item in search_results
            ]
            
            query_time = time.time() - start_time
            logger.info(f"查询完成，耗时: {query_time:.2f}秒")
            
            return {
                "answer": answer,
                "sources": sources,
                "query_time": query_time
            }
            
        except Exception as e:
            logger.error(f"RAG查询失败: {e}")
            raise
    
    def query_with_pipeline(
        self,
        query: str,
        conversation_id:  Optional[str] = None,
        history_turns: int = 5,
        current_date:     Optional[str] = None,
        top_k: int = 5,
        filter_source:    Optional[str] = None,
        filter_category:  Optional[str] = None,
        filter_date_from: Optional[str] = None,
        filter_date_to:   Optional[str] = None
    ) -> Dict:
        """
        使用 Pipeline 执行RAG查询（支持多轮对话）
        
        Args:
            query: 查询问题
            conversation_id: 对话ID（用于获取历史和状态管理）
            history_turns: 获取最近N轮历史
            current_date: 当前日期
            top_k: 返回top-k结果
            filter_source: 过滤来源
            filter_category: 过滤类别
            filter_date_from: 过滤起始日期
            filter_date_to: 过滤结束日期
            
        Returns:
            查询响应，包含answer、sources、query_time、classification、route_decision等
        """
        from app.services.schemas import PipelineInput
        
        # 构建 Pipeline 输入
        input_data = PipelineInput(
            query=query,
            conversation_id=conversation_id,
            history_turns=history_turns,
            current_date=current_date,
            top_k=top_k,
            filter_source=filter_source,
            filter_category=filter_category,
            filter_date_from=filter_date_from,
            filter_date_to=filter_date_to
        )
        
        # 执行 Pipeline
        output = self.pipeline.run(input_data)
        
        # 转换为字典格式（兼容旧接口）
        return {
            "answer": output.answer,
            "sources": output.sources,
            "query_time": output.query_time,
            # 新增字段
            "classification": output.classification.model_dump(),
            "route_decision": output.route_decision.model_dump(),
            "standalone_query": output.standalone_query
        }

    def query_with_pipeline_stream(
        self,
        query: str,
        conversation_id:  Optional[str] = None,
        history_turns: int = 5,
        current_date:     Optional[str] = None,
        top_k: int = 5,
        filter_source:    Optional[str] = None,
        filter_category:  Optional[str] = None,
        filter_date_from: Optional[str] = None,
        filter_date_to:   Optional[str] = None
    ) -> Iterator[Dict]:
        """流式执行 Pipeline，yield SSE 事件 dict（与 OpenAI 兼容的 content / replace / done）。"""
        from app.services.schemas import PipelineInput
        input_data = PipelineInput(
            query=query,
            conversation_id=conversation_id,
            history_turns=history_turns,
            current_date=current_date,
            top_k=top_k,
            filter_source=filter_source,
            filter_category=filter_category,
            filter_date_from=filter_date_from,
            filter_date_to=filter_date_to
        )
        yield from self.pipeline.run_stream(input_data)
