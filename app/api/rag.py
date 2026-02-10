# app/api/rag.py
"""
RAG API接口 - 意图判断和向量库搜索
重构后支持 Pipeline 模式，保持旧接口兼容；支持流式输出（SSE）。
"""
import json
from flask import Blueprint, request, current_app, Response, stream_with_context
from app.utils.response import success_response, error_response
from app.utils.jwt_auth import verify_token
from loguru import logger

rag_bp = Blueprint('rag', __name__, url_prefix='/api/rag')

# 服务实例（延迟初始化，避免循环导入）
_rag_service = None


def _get_rag_service():
    """获取RAG服务实例（延迟初始化）"""
    global _rag_service
    if _rag_service is None:
        from app.services.rag_service import RAGService
        _rag_service = RAGService()
    return _rag_service


def _get_openid_from_token():
    """从请求头中获取并验证JWT token，返回openid"""
    auth_header = request.headers.get('Authorization')
    if not auth_header:
        return None
    try:
        token = auth_header.split(' ')[1] if ' ' in auth_header else auth_header
        return verify_token(token)
    except Exception:
        return None


@rag_bp.route('/intent', methods=['POST'])
def check_intent():
    """
    POST /api/rag/intent
    [仅供调试/内部使用] 意图判断接口
    
    注意：主流程已迁移至统一入口 POST /api/chat，
    该接口仅保留用于调试面板和内部诊断，前端主流程不再调用。
    
    请求体：
    {
        "query": "用户问题",
        "current_date": "2026-02-04",       // 可选，默认当前日期
        "conversation_id": "chat_123",      // 可选，用于多轮对话
        "history_turns": 5,                 // 可选，获取最近N轮历史
        "use_pipeline": true                // 可选，是否使用新Pipeline（默认true）
    }
    
    响应：
    {
        "code": 200,
        "data": {
            "needs_search": true/false,
            "intent_type": "新闻/实时行情/常识问答/...",
            "category": "贵金属/科技/常识/...",
            "core_claim": "核心问题",
            "standalone_query": "改写后的独立查询",  // 新增
            "confidence": 0.85,                      // 新增
            ...
        }
    }
    """
    # 验证登录状态
    openid = _get_openid_from_token()
    if not openid:
        return error_response('未授权，请先登录', 401)
    
    data = request.get_json()
    if not data:
        return error_response('请求体不能为空', 400)
    
    query = data.get('query', '').strip()
    if not query:
        return error_response('query不能为空', 400)
    
    current_date = data.get('current_date')
    conversation_id = data.get('conversation_id')
    history_turns = data.get('history_turns', 5)
    use_pipeline = data.get('use_pipeline', True)
    
    try:
        rag_service = _get_rag_service()
        
        if use_pipeline:
            # 使用新 Pipeline
            result = rag_service.check_intent_with_pipeline(
                query=query,
                conversation_id=conversation_id,
                history_turns=history_turns,
                current_date=current_date
            )
        else:
            # 降级到旧实现（如果需要）
            from app.services.intent_service import IntentService
            intent_service = IntentService()
            result = intent_service.check_intent(query, current_date)
        
        return success_response(result)
        
    except Exception as e:
        logger.error(f"意图判断失败: {e}")
        return error_response(f'意图判断失败: {str(e)}', 500)


@rag_bp.route('/query', methods=['POST'])
def rag_query():
    """
    POST /api/rag/query
    向量库搜索接口：执行RAG查询
    
    请求体：
    {
        "query": "用户问题",
        "top_k": 5,                         // 可选，返回top-k结果，默认5
        "filter_category": "economy",       // 可选，过滤类别: academic/world/tech/economy/sports/general/health
        "filter_source": "新浪财经",         // 可选，过滤来源
        "filter_date_from": "2026-02-01",   // 可选，起始日期
        "filter_date_to": "2026-02-04",     // 可选，结束日期
        "conversation_id": "chat_123",      // 可选，用于多轮对话
        "history_turns": 5,                 // 可选，获取最近N轮历史
        "current_date": "2026-02-05",       // 可选，当前日期
        "use_pipeline": true                // 可选，是否使用新Pipeline（默认true）
    }
    
    响应：
    {
        "code": 200,
        "data": {
            "answer": "AI生成的回答",
            "sources": [...],
            "query_time": 2.5,
            "classification": {...},         // 新增（use_pipeline=true时）
            "route_decision": {...},         // 新增（use_pipeline=true时）
            "standalone_query": "..."        // 新增（use_pipeline=true时）
        }
    }
    """
    # 验证登录状态
    openid = _get_openid_from_token()
    if not openid:
        return error_response('未授权，请先登录', 401)
    
    data = request.get_json()
    if not data:
        return error_response('请求体不能为空', 400)
    
    query = data.get('query', '').strip()
    if not query:
        return error_response('query不能为空', 400)
    
    # 解析通用参数
    top_k = data.get('top_k', 5)
    if not isinstance(top_k, int) or top_k < 1:
        top_k = 5
    if top_k > 20:
        top_k = 20
    
    filter_category = data.get('filter_category')
    filter_source = data.get('filter_source')
    filter_date_from = data.get('filter_date_from')
    filter_date_to = data.get('filter_date_to')
    
    # 新增参数
    conversation_id = data.get('conversation_id')
    history_turns = data.get('history_turns', 5)
    current_date = data.get('current_date')
    use_pipeline = data.get('use_pipeline', True)
    stream = data.get('stream', False)

    try:
        rag_service = _get_rag_service()

        if stream and use_pipeline:
            def generate():
                for event in rag_service.query_with_pipeline_stream(
                    query=query,
                    conversation_id=conversation_id,
                    history_turns=history_turns,
                    current_date=current_date,
                    top_k=top_k,
                    filter_source=filter_source,
                    filter_category=filter_category,
                    filter_date_from=filter_date_from,
                    filter_date_to=filter_date_to
                ):
                    yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"

            return Response(
                stream_with_context(generate()),
                mimetype="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                }
            )

        if use_pipeline:
            result = rag_service.query_with_pipeline(
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
        else:
            result = rag_service.query(
                query=query,
                top_k=top_k,
                filter_source=filter_source,
                filter_category=filter_category,
                filter_date_from=filter_date_from,
                filter_date_to=filter_date_to
            )

        return success_response(result)

    except Exception as e:
        logger.error(f"RAG查询失败: {e}")
        return error_response(f'RAG查询失败: {str(e)}', 500)


@rag_bp.route('/info', methods=['GET'])
def get_info():
    """
    GET /api/rag/info
    获取向量库信息
    
    响应：
    {
        "code": 200,
        "data": {
            "name": "news_collection",
            "points_count": 1000,
            "vectors_count": 1000
        }
    }
    """
    try:
        rag_service = _get_rag_service()
        info = rag_service.vector_store.get_collection_info()
        return success_response(info)
    except Exception as e:
        logger.error(f"获取向量库信息失败: {e}")
        return error_response(f'获取向量库信息失败: {str(e)}', 500)


@rag_bp.route('/pipeline/stats', methods=['GET'])
def get_pipeline_stats():
    """
    GET /api/rag/pipeline/stats
    获取 Pipeline 性能统计（用于监控）
    
    响应：
    {
        "code": 200,
        "data": {
            "count": 100,
            "total_ms": {"avg": 1500, "p50": 1200, "p95": 3000, "p99": 5000},
            "rewrite_ms": {"avg": 100, "p50": 80},
            "classify_ms": {"avg": 150, "p50": 120}
        }
    }
    """
    # 验证登录状态
    openid = _get_openid_from_token()
    if not openid:
        return error_response('未授权，请先登录', 401)
    
    try:
        from app.services.pipeline_logger import get_pipeline_logger
        pipeline_logger = get_pipeline_logger()
        
        hours = request.args.get('hours', 24, type=int)
        stats = pipeline_logger.get_latency_stats(hours=hours)
        
        return success_response(stats)
        
    except Exception as e:
        logger.error(f"获取Pipeline统计失败: {e}")
        return error_response(f'获取统计失败: {str(e)}', 500)
