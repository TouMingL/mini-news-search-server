# app/services/intent_service.py
"""
[已废弃] 意图判断服务 - 判断用户问题是否需要搜索

警告：此模块已废弃，仅保留用于向后兼容。
新代码请使用 Pipeline 架构：
    from app.services.pipeline import get_pipeline
    pipeline = get_pipeline()
    result = pipeline.intent_only(query, conversation_id)

或使用 RAGService 的 Pipeline 方法：
    from app.services.rag_service import RAGService
    rag_service = RAGService()
    result = rag_service.check_intent_with_pipeline(query, conversation_id)

新架构优势：
1. 支持多轮对话上下文
2. 指代消解和省略补全
3. 本地模型低延迟分类
4. FSM状态机路由决策
5. 完整的流程日志
"""
import json
import warnings
from datetime import datetime
from typing import Dict, Optional
from loguru import logger

from app.services.llm_service import LLMService


class IntentService:
    """意图判断服务，判断用户问题是否需要向量库搜索"""
    
    # 前置思考prompt模板：以「是否属于新闻范围、常识能否回答」为核心
    PRETHINK_PROMPT = """你是一个意图判断助手。请判断用户问题是否**必须依赖新闻/向量库**才能回答。

**核心原则：只要凭你的常识（通用知识、百科、概念、历史常识）能回答的问题，一律判定为不需要搜索。**

今天日期：{current_date}
用户输入：{user_input}

**needs_search 判定规则：**
- 填 **false**（不调用搜索）：能用常识/通用知识回答的，包括但不限于：概念解释、百科知识、历史常识、科学原理、数学计算、打招呼、闲聊、非时效性的一般性问题。例如：「什么是GDP」「太阳为什么发光」「地球到月球多远」「光合作用是什么」「你好」「1+1等于几」。
- 填 **true**（需要调用新闻/向量库）：明确属于**新闻范围**、必须查近期报道或外部数据才能回答的，例如：最近/今日/本周的赛事、行情、政策、事件、某日某地发生的事、某某最新进展、某某最新价格、近期热点。例如：「最近有什么体育赛事」「今天金价多少」「某某公司最新新闻」「昨日某地发生了什么」。

**输出要求：** 严格 JSON，仅包含以下字段，无多余说明。
1. needs_search：布尔值（true/false）。按上面规则：常识能答填 false，属于新闻/需查近期资料填 true。
2. intent_type：字符串。needs_search=false 时填 "常识问答" 或 "no_search"；needs_search=true 时填 "新闻" 或 "实时行情" 等。
3. category：字符串，仅限枚举值：贵金属/能源/股指/外汇/农产品/天气/宏观/科技/政治/社会/体育/常识/其他。
4. core_claim：字符串。用户核心问题一句话，不超过50字。
5. is_historical：布尔值。用户是否问了过去某具体日期/时段。
6. time_window：字符串。用户提到的时间范围，无则留空。
7. resolved_date：字符串。能解析出的具体日期则填 YYYY年M月D日，否则留空。
8. scope：字符串。global/domestic/both/local 或留空。
9. key_metrics：字符串。用户关心的指标，无则留空。"""
    
    def __init__(self, llm_service: LLMService = None):
        warnings.warn(
            "IntentService 已废弃，请使用新的 Pipeline 架构。"
            "参见: from app.services.pipeline import get_pipeline",
            DeprecationWarning,
            stacklevel=2
        )
        self.llm_service = llm_service or LLMService()
    
    def _extract_json(self, text: str) -> Optional[Dict]:
        """解析文本中的 JSON 对象或数组"""
        text = (text or "").strip()
        # 去掉 markdown 代码块
        if "```" in text:
            start_m = text.find("```")
            if start_m >= 0:
                rest = text[start_m + 3:]
                if rest.startswith("json"):
                    rest = rest[4:].lstrip()
                end_m = rest.find("```")
                text = rest[:end_m].strip() if end_m >= 0 else rest
        # 先尝试整体解析（可能是对象或数组）
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        # 再尝试从第一个 { 或 [ 开始匹配完整结构
        for start_char, end_char in (("{", "}"), ("[", "]")):
            start = text.find(start_char)
            if start < 0:
                continue
            depth = 0
            for i, c in enumerate(text[start:], start):
                if c == start_char:
                    depth += 1
                elif c == end_char:
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[start : i + 1])
                        except json.JSONDecodeError:
                            pass
                        break
        return None
    
    def check_intent(self, user_input: str, current_date: str = None) -> Dict:
        """
        判断用户问题的意图，决定是否需要搜索
        
        Args:
            user_input: 用户输入的问题
            current_date: 当前日期（YYYY-MM-DD格式），如果不提供则自动获取
            
        Returns:
            意图分析结果，包含 needs_search、intent_type、category 等字段
        """
        if current_date is None:
            current_date = datetime.now().strftime("%Y-%m-%d")
        
        prompt = self.PRETHINK_PROMPT.replace("{current_date}", current_date).replace("{user_input}", user_input)
        
        try:
            response = self.llm_service.chat([{"role": "user", "content": prompt}])
            text = (response or "").strip()
            logger.info(f"意图判断原始响应: {text[:500]}...")
            
            parsed = self._extract_json(text)
            data = parsed if isinstance(parsed, dict) else {}
            
            # 处理 needs_search 字段
            needs_search = data.get("needs_search", True)
            if isinstance(needs_search, str):
                needs_search = needs_search.lower() in ("true", "1", "yes")
            data["needs_search"] = needs_search
            
            # 确保必要字段存在
            data.setdefault("intent_type", "no_search" if not needs_search else "新闻")
            data.setdefault("category", "其他")
            data.setdefault("core_claim", user_input[:50])
            data.setdefault("is_historical", False)
            data.setdefault("time_window", "")
            data.setdefault("resolved_date", "")
            data.setdefault("scope", "both")
            data.setdefault("key_metrics", "")
            
            logger.info(f"意图判断结果: needs_search={data['needs_search']}, intent_type={data['intent_type']}, category={data['category']}")
            
            return data
            
        except Exception as e:
            logger.error(f"意图判断失败: {e}")
            # 失败时默认需要搜索
            return {
                "needs_search": True,
                "intent_type": "新闻",
                "category": "其他",
                "core_claim": user_input[:50],
                "is_historical": False,
                "time_window": "",
                "resolved_date": "",
                "scope": "both",
                "key_metrics": "",
                "error": str(e)
            }
