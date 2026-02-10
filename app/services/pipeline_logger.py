# app/services/pipeline_logger.py
"""
反馈层 - Pipeline Logger
职责：记录全流程日志，支持后续分析和优化
"""
import json
import uuid
from datetime import datetime
from typing import Optional, List, Dict, Any
from pathlib import Path
from loguru import logger

from app.services.schemas import (
    PipelineLog,
    LatencyMetrics,
    ClassificationResult,
    RouteDecision
)


class PipelineLogger:
    """
    Pipeline 日志记录器
    记录每次请求的完整流程数据，用于：
    1. 问题排查
    2. 性能分析
    3. 误判case收集
    4. A/B测试数据
    """
    
    def __init__(self, log_dir: str = "logs/pipeline"):
        """
        初始化日志记录器
        
        Args:
            log_dir: 日志目录
        """
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        
        # 内存缓冲（批量写入优化）
        self._buffer: List[PipelineLog] = []
        self._buffer_size = 100
    
    def create_request_id(self) -> str:
        """生成唯一请求ID"""
        return str(uuid.uuid4())[:8]
    
    def log(
        self,
        request_id: str,
        conversation_id: Optional[str],
        raw_input: str,
        standalone_query: str,
        classification: ClassificationResult,
        route_decision: RouteDecision,
        retrieval_count: int,
        final_response: str,
        latency: LatencyMetrics,
        error: Optional[str] = None
    ) -> PipelineLog:
        """
        记录一次完整的 Pipeline 执行日志
        
        Args:
            request_id: 请求ID
            conversation_id: 对话ID
            raw_input: 原始用户输入
            standalone_query: 改写后的独立查询
            classification: 分类结果
            route_decision: 路由决策
            retrieval_count: 检索结果数量
            final_response: 最终响应
            latency: 各阶段耗时
            error: 错误信息（如有）
            
        Returns:
            PipelineLog 实例
        """
        log_entry = PipelineLog(
            request_id=request_id,
            conversation_id=conversation_id,
            raw_input=raw_input,
            standalone_query=standalone_query,
            classification=classification.model_dump(),
            route_decision=route_decision.action,
            retrieval_count=retrieval_count,
            final_response=final_response[:500] if final_response else "",  # 截断
            latency=latency,
            timestamp=datetime.now(),
            error=error
        )
        
        # 输出到 loguru
        log_summary = (
            f"[Pipeline] request={request_id} | "
            f"route={route_decision.action} | "
            f"needs_search={classification.needs_search} | "
            f"filter_category={classification.filter_category} | "
            f"retrieval={retrieval_count} | "
            f"total_ms={latency.total_ms:.1f}"
        )
        
        if error:
            logger.error(f"{log_summary} | error={error}")
        else:
            logger.info(log_summary)
        
        # 添加到缓冲
        self._buffer.append(log_entry)
        
        # 缓冲满时写入文件
        if len(self._buffer) >= self._buffer_size:
            self._flush_buffer()
        
        return log_entry
    
    def _flush_buffer(self):
        """将缓冲区日志写入文件"""
        if not self._buffer:
            return
        
        # 按日期分文件
        today = datetime.now().strftime("%Y-%m-%d")
        log_file = self.log_dir / f"pipeline_{today}.jsonl"
        
        try:
            with open(log_file, "a", encoding="utf-8") as f:
                for entry in self._buffer:
                    f.write(entry.model_dump_json() + "\n")
            
            logger.debug(f"写入 {len(self._buffer)} 条日志到 {log_file}")
            self._buffer.clear()
            
        except Exception as e:
            logger.error(f"写入日志文件失败: {e}")
    
    def flush(self):
        """强制刷新缓冲区"""
        self._flush_buffer()
    
    def get_recent_logs(
        self,
        limit: int = 100,
        conversation_id: Optional[str] = None
    ) -> List[PipelineLog]:
        """
        获取最近的日志（用于调试）
        
        Args:
            limit: 最大数量
            conversation_id: 过滤特定对话
            
        Returns:
            PipelineLog 列表
        """
        logs = []
        
        # 从最新的文件开始读
        log_files = sorted(self.log_dir.glob("pipeline_*.jsonl"), reverse=True)
        
        for log_file in log_files:
            try:
                with open(log_file, "r", encoding="utf-8") as f:
                    for line in f:
                        if not line.strip():
                            continue
                        entry = PipelineLog.model_validate_json(line)
                        if conversation_id and entry.conversation_id != conversation_id:
                            continue
                        logs.append(entry)
                        if len(logs) >= limit:
                            return logs
            except Exception as e:
                logger.warning(f"读取日志文件失败: {log_file}, {e}")
        
        return logs
    
    def get_error_logs(self, limit: int = 50) -> List[PipelineLog]:
        """获取最近的错误日志"""
        all_logs = self.get_recent_logs(limit * 10)
        error_logs = [log for log in all_logs if log.error]
        return error_logs[:limit]
    
    def get_latency_stats(self, hours: int = 24) -> Dict[str, Any]:
        """
        获取延迟统计（用于性能监控）
        
        Args:
            hours: 统计时间范围（小时）
            
        Returns:
            延迟统计字典
        """
        from datetime import timedelta
        cutoff = datetime.now() - timedelta(hours=hours)
        
        logs = self.get_recent_logs(limit=10000)
        recent_logs = [log for log in logs if log.timestamp > cutoff]
        
        if not recent_logs:
            return {"count": 0}
        
        total_latencies = [log.latency.total_ms for log in recent_logs]
        rewrite_latencies = [log.latency.rewrite_ms for log in recent_logs]
        classify_latencies = [log.latency.classify_ms for log in recent_logs]
        
        def percentile(data, p):
            if not data:
                return 0
            sorted_data = sorted(data)
            k = (len(sorted_data) - 1) * p / 100
            f = int(k)
            c = f + 1 if f + 1 < len(sorted_data) else f
            return sorted_data[f] + (sorted_data[c] - sorted_data[f]) * (k - f)
        
        return {
            "count": len(recent_logs),
            "total_ms": {
                "avg": sum(total_latencies) / len(total_latencies),
                "p50": percentile(total_latencies, 50),
                "p95": percentile(total_latencies, 95),
                "p99": percentile(total_latencies, 99)
            },
            "rewrite_ms": {
                "avg": sum(rewrite_latencies) / len(rewrite_latencies),
                "p50": percentile(rewrite_latencies, 50)
            },
            "classify_ms": {
                "avg": sum(classify_latencies) / len(classify_latencies),
                "p50": percentile(classify_latencies, 50)
            }
        }


# 工厂函数
_pipeline_logger_instance: Optional[PipelineLogger] = None


def get_pipeline_logger() -> PipelineLogger:
    """获取 PipelineLogger 单例"""
    global _pipeline_logger_instance
    if _pipeline_logger_instance is None:
        _pipeline_logger_instance = PipelineLogger()
    return _pipeline_logger_instance
