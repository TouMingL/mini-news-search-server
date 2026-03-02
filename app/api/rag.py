# app/api/rag.py
"""
RAG API：向量库搜索与 Pipeline 查询；统一对话入口为 POST /api/chat。
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


@rag_bp.route('/query', methods=['POST'])
def rag_query():
    """
    POST /api/rag/query
    执行 RAG 查询（Pipeline）；支持流式。
    请求体：query, top_k, filter_*, conversation_id, history_turns, current_date, stream
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
    stream = data.get('stream', False)

    try:
        rag_service = _get_rag_service()

        if stream:
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
