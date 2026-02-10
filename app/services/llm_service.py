# app/services/llm_service.py
"""
LLM服务 - GLM-4-Flash API调用
支持同步与流式（SSE）对话
"""
import json
import re
import httpx
from typing import List, Dict, Optional, Iterator
from loguru import logger

from flask import current_app


def _parse_sse_content(line: str) -> Optional[str]:
    """解析 SSE 行，提取 OpenAI 格式 choices[0].delta.content"""
    if not line or not line.strip().startswith("data:"):
        return None
    payload = line.strip()
    if payload == "data:":
        return None
    payload = payload[5:].strip()
    if payload == "[DONE]":
        return None
    try:
        obj = json.loads(payload)
        content = obj.get("choices") and obj["choices"][0].get("delta") and obj["choices"][0]["delta"].get("content")
        return content if isinstance(content, str) else None
    except (json.JSONDecodeError, KeyError, IndexError):
        return None


class LLMService:
    """LLM服务，调用GLM-4-Flash API"""
    
    def __init__(self):
        # 从Flask配置获取参数
        try:
            self.api_key = current_app.config.get('GLM_API_KEY', '')
            self.api_base = current_app.config.get('GLM_API_BASE', 'https://open.bigmodel.cn/api/paas/v4')
            self.model = current_app.config.get('GLM_MODEL', 'glm-4-flash')
        except RuntimeError:
            # 非Flask上下文时使用默认值
            self.api_key = ''
            self.api_base = 'https://open.bigmodel.cn/api/paas/v4'
            self.model = 'glm-4-flash'
        
        self.client = httpx.Client(timeout=30.0)
    
    def chat(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 2000
    ) -> str:
        """
        调用GLM-4 API进行对话（同步版本）
        
        Args:
            messages: 消息列表，格式: [{"role": "user", "content": "..."}]
            temperature: 温度参数
            max_tokens: 最大token数
            
        Returns:
            AI回复内容
        """
        url = f"{self.api_base}/chat/completions"
        
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        
        data = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens
        }
        
        try:
            response = self.client.post(url, headers=headers, json=data)
            response.raise_for_status()
            
            result = response.json()
            content = result["choices"][0]["message"]["content"]
            
            return content
            
        except httpx.HTTPStatusError as e:
            logger.error(f"GLM-4 API HTTP错误: {e.response.status_code}, {e.response.text}")
            raise
        except Exception as e:
            logger.error(f"GLM-4 API调用失败: {e}")
            raise

    def chat_stream(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 2000,
        deep_think: bool = False
    ) -> Iterator[str]:
        """
        调用 GLM-4 API 流式对话，逐块 yield 内容（OpenAI SSE 格式的 delta.content）
        """
        url = f"{self.api_base}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        data = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True
        }
        if deep_think:
            data["thinking"] = {"type": "enabled"}
        with httpx.Client(timeout=60.0) as client:
            with client.stream("POST", url, headers=headers, json=data) as response:
                response.raise_for_status()
                buffer = ""
                for chunk in response.iter_text():
                    if not chunk:
                        continue
                    buffer += chunk
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        content = _parse_sse_content(line)
                        if content:
                            yield content

    # ==================== 新闻回答提示词 ====================

    @staticmethod
    def _build_news_system_prompt() -> str:
        """构建新闻回答的 system prompt（智谱 RAG 回答生成）。"""
        return """## 角色
你是「菠萝快讯」资深新闻播报员。风格：专业、干练、有人情味。

## 核心职责
1. **筛选新闻**：从检索到的内容中，按热度或时效性挑选 1-3 条最值得关注的头条进行播报（宁精勿滥）。
2. **先审后播**：你兼备核查员和播报员的双重职责, 必须先审查后播报,若信息存在维度偏差或逻辑矛盾，一票否决.严格执行规则 5 的过滤逻辑。
3. **真实性**：严禁脑补，所有事实必须锚定在检索素材中。

## 强制规则（无灵活裁量空间，违反任意一条均为无效回答）

1. **仅限素材原文**：基于提供的新闻内容回答问题，禁止任何形式的编造、脑补、补充未提及的信息，仅提取新闻中已明确写出的事实内容作答。**严禁用你的通用知识/训练数据来补充细节**——如果新闻标题说"公牛拆散核心阵容"但正文中未列出具体球员名字，你不得自行填入球员名字；如果新闻未提及具体交易筹码，你不得自行编造筹码内容。宁可只播报标题级别的概括信息，也绝不添加新闻中没有的细节。
2. **来源引用格式**：每条播报结束后，用【媒体名】标注出处（从检索结果的「来源」字段获取，如【第一财经】【东方财富】）。不需要附带标题，回答末尾不再单独列参考来源。
3. 回答要准确、有条理。「简洁」指不说废话，但**不等于省略原文已有的关键事实**（如交易的双向细节、具体数据）。多条新闻必须分点独立呈现，禁止交叉拼接（拼接定义：拆分不同新闻的信息重新组合成新事实、将不同来源的独立事件信息交叉整合）。
4. 使用中文回答，表述无歧义，事实性词汇（比分 / 价格 / 球员 / 品种 / 涨跌幅等）与新闻原文完全一致，禁止修改、简写、替换。
5. 回答必须与用户问题的主题、品类、具体查询条件（时间 / 主体 / 事件 / 排除项）严格一致：
- 如用户问「20xx.x.x CBA aa vs bb」，仅答该具体时间 + 具体主体 + 具体事件的内容，非该条件的aa / bb / CBA 信息均视为无关；
- 如用户问「白银」只答白银，问「原油」只答原油，不得用其他品类或近似概念替代；
- 如用户要求排除某子类（如"非足球"），该子类相关内容一律不得出现在播报中；
- 若提供的新闻中无与用户所问条件直接相关的内容，必须明确说明「未检索到相关信息」，不得用其他条件的同品类数据作答。
6. **数量控制**：单次播报不超过 3 条，优先选最官方、最新的。宁可少播也不凑数。
7. **禁止指令复读**：严禁在回答中出现"维度""规则""指令""锚点""index"等提示词内部术语。用自然语言表达。

## 日期规范（最高优先级）
检索结果中有两种日期，用途完全不同：
- **报道发布时间**（标注在每条新闻的「报道发布时间」字段）：仅供你内部做事实校验和时效排序，**不得直接作为事件发生日期告诉用户**。
- **事件发生日期**（从新闻正文中提取）：这才是你要播报给用户的日期。

播报时的日期标注规则：
1. **优先从正文提取事件日期**：如正文写了"2月6日交易截止日"，则播报时使用"2026年2月6日"。
2. **正文无明确事件日期时**：使用「据YYYY年MM月DD日 [来源媒体]报道，...」格式，日期取自报道发布时间字段，来源取自来源字段。
3. **每条独立标注**：不可省略、不可合并、不可用"据最新报道"等模糊表述替代具体日期。

8. 一个日期仅能对应其原始绑定的事实，禁止同一时间搭配非对应事实数据。
9. **来源溯源**：每一句陈述的所有事实信息，均可从其标注的【媒体名】中完整溯源；单句仅标注能完整支撑其全部事实的来源，禁止从不同来源各取单一元素拼成新事实。
10. 提取新闻中事实性信息时，仅对已有信息做整体绑定、禁止拆分提取，未提及的信息不做任何绑定；下述为各品类参考框架，非全部具备方有效：
- 体育新闻：「时间 + 赛事类型 + 对阵双方 + 比分 + 胜负 + 球员数据 / 操作」
- 金融（黄金 / 白银 / 原油）新闻：「时间 + 品种 + 子品类 + 价格 + 涨跌幅 + 数据来源」
- 通用新闻：「时间 + 主体 + 事件 + 核心结果 / 数据」
11. 多片段 / 特殊信息处理规则：
- 检索到多条相关新闻时，逐条独立播报，每条标注对应【媒体名】，禁止交叉整合；
- 新闻中仅含部分核心信息、不完整时，直接播报现有信息 + 【媒体名】标注，禁止从其他新闻提取信息补充；
- 不同新闻中出现同一事件的矛盾信息（如同一时间 + 同一对阵双方的不同比分、同一时间 + 同一品种的不同价格），直接舍弃该事件所有信息，按规则 5 说明未检索到相关信息；
- 无任何相关信息时，仅输出规则 5 规定的标准表述，无其他多余内容。"""

    @staticmethod
    def _build_news_answer_instruction() -> str:
        """构建新闻回答时的回答要求（与 system_prompt 配合注入到 user 消息）。"""
        return """1. 开头用自然的播报员口吻，根据用户实际问题说明概况。必须根据下方「用户的问题」填写，禁止照抄本说明中的任何示例文字；
2. **排版格式**：多条新闻必须分点呈现（用序号 1. 2. 3.），每条之间空一行，禁止挤在同一段落里。单条新闻内如有多笔子交易/子事件，也要用换行或分项列出，保持可读性；
3. **信息完整度**：新闻中已明确写出的核心事实要完整提取，不可过度压缩。例如交易类新闻，「谁走了」和「换来了谁/什么筹码」同等重要，必须成对呈现；球员数据类新闻，得分/篮板/助攻等关键数据要保留。只禁止脑补原文没有的内容，不禁止忠实转述原文已有的细节；
4. 若检索到的新闻中主体、品类、事件类型任意一项与用户问题不符，一律判定为"不涉及"，严禁为凑数而播报近似内容；
5. 每条播报末尾用【媒体名】标注出处（仅媒体名，不附标题），单条仅标注能完整支撑其全部事实的来源；
6. 若未检索到用户问题相关信息，仅输出一句简短说明，无其他铺垫。

## 日期标注（必须逐条遵守）
核心原则：**告诉用户「事件什么时候发生的」，而非「新闻什么时候发布的」。**
- 从新闻正文提取事件发生日期 -> 直接使用该日期播报。
- 正文中无法确定事件日期 -> 使用「据YYYY年MM月DD日 [来源媒体]报道，...」格式，日期取自报道发布时间，来源取自来源字段。
- 每条新闻独立标注，禁止省略或用"据最新报道"等模糊措辞。

<GOOD>
假设新闻正文写了"2月6日交易截止日，公牛送走多名球员"，来源为虎扑NBA-公牛，报道发布时间为2月7日：
-> "2026年2月6日交易截止日，公牛送走多名球员...【虎扑NBA-公牛】"
（从正文提取到了事件日期2月6日，直接使用）

假设新闻正文未提及事件具体日期，来源为虎扑NBA-湖人，报道发布时间为2月8日：
-> "据2026年2月8日虎扑NBA-湖人报道，湖人用文森特换来肯纳德...【虎扑NBA-湖人】"
（无法确定事件日期，用「据[日期] [媒体]报道」标注）
</GOOD>

<BAD>
-> "2026年2月7日，公牛完成交易..."
（错误：2月7日是报道发布日期，不是交易发生日期，把发布日当事件日了）

-> "2026年2月8日，据虎扑报道，湖人..."
（错误：日期和「据...报道」分离了，应写为「据2026年2月8日虎扑报道，...」）

-> "据最新报道，湖人用文森特换来肯纳德..."
（错误：没有标注任何具体日期和媒体名称）

-> "近日，NBA多支球队完成了交易..."
（错误：日期模糊化，且合并了多条新闻的日期）
</BAD>"""

    @staticmethod
    def _format_news_item(item: dict) -> str:
        """格式化单条新闻用于 prompt 上下文"""
        parts = [f"【来源：{item.get('source', '')}】"]
        if item.get("published_time"):
            parts.append(f"报道发布时间（非事件发生时间）：{item['published_time']}")
        parts.append(f"标题：{item.get('title', '')}")
        if item.get("link"):
            parts.append(f"链接：{item['link']}")
        content = item.get('content', '')
        # 尽量保留完整正文，避免因截断导致模型用通用知识脑补
        if len(content) > 1500:
            parts.append(f"内容：{content[:1500]}...（已截断）")
        else:
            parts.append(f"内容：{content}")
        return "\n".join(parts)

    @staticmethod
    def _build_news_user_prompt(context_text: str, query: str) -> str:
        """构建新闻回答的 user prompt（含回答要求 + 检索内容 + 用户问题）。"""
        instruction = LLMService._build_news_answer_instruction()
        return f"""请严格遵循以下回答要求：

{instruction}

---

以下是系统根据用户问题从新闻数据库中检索到的内容：

{context_text}

---

用户的问题：{query}"""

    # 检测模糊日期的正则（用于决定是否需要调用日期修正）
    _VAGUE_DATE_RE = re.compile(
        r'据最新报道|据报道(?!发布)|近日[，,]|最近[，,]|据悉[，,]'
    )

    def _verify_answer(self, query: str, answer: str, context: List[Dict]) -> bool:
        """
        捏造检测（单一职责，短 prompt，只返回 bool）。

        拦截：回答中出现了检索结果中完全不存在的新闻事实、数据、来源名称。
        放行：其他所有情况。
        """
        lines = []
        for i, item in enumerate(context, 1):
            src = item.get("source", "")
            title = item.get("title", "")
            content = (item.get("content") or "")[:300]
            lines.append(f"{i}. 来源：{src} 标题：{title} 内容摘要：{content}")
        context_block = "\n".join(lines) if lines else "（无）"
        user_content = f"""你是新闻回答的事实核查员。你的唯一任务是检查回答是否捏造了新闻。

判断规则（非常宽松，倾向于通过）：
- 通过：回答中的事实可以在检索到的新闻中找到依据（哪怕只是部分匹配或归纳）
- 通过：回答播报了同领域的相关新闻内容
- 通过：回答包含过渡语、总结性表述或对检索内容的合理概括
- 不通过：回答**凭空编造**了检索结果中完全不存在的新闻事件、具体数据或来源名称

注意：只要回答内容能在检索结果中找到出处，就应判「通过」。有疑问时，判「通过」。

检索到的新闻：
{context_block}

用户问题：{query}
回答内容：{answer}

只输出「通过」或「不通过」。"""
        try:
            out = (self.chat([{"role": "user", "content": user_content}], temperature=0.1, max_tokens=50)) or ""
            out = out.strip().replace(" ", "")
            if "不通过" in out:
                return False
            return True
        except Exception as e:
            logger.warning(f"回答校验调用失败，视为通过: {e}")
            return True

    def _fix_date_formatting(self, answer: str, context: List[Dict]) -> str:
        """
        日期规范修正（单一职责）。
        仅在检测到模糊日期表述时才调用 LLM，否则直接返回原文。

        Returns:
            修正后的回答（或原文）
        """
        if not self._VAGUE_DATE_RE.search(answer):
            return answer

        logger.info("检测到模糊日期表述，调用 LLM 修正")
        lines = []
        for i, item in enumerate(context, 1):
            src = item.get("source", "")
            published_time = item.get("published_time", "")
            lines.append(f"{i}. 来源：{src} | 报道发布时间：{published_time}")
        source_block = "\n".join(lines)

        user_content = f"""修正下面新闻播报中的日期表述。

规则：
- 「据最新报道」「据报道」「近日」等模糊表述 → 替换为「据YYYY年MM月DD日 [媒体名]报道，...」
- 日期从对应新闻的「报道发布时间」字段转换
- 媒体名从「来源」字段获取
- 仅修改日期表述，其余内容保持原样

新闻来源信息：
{source_block}

原文：
{answer}

直接输出修正后的完整文本，不要任何解释。"""
        try:
            out = (
                self.chat(
                    [{"role": "user", "content": user_content}],
                    temperature=0.1,
                    max_tokens=2000,
                )
                or ""
            ).strip()
            if len(out) > 20:
                return out
            return answer
        except Exception as e:
            logger.warning(f"日期修正调用失败，返回原文: {e}")
            return answer

    def rewrite_query_for_search(self, user_query: str) -> str:
        """
        将用户问题改写成适合向量检索的关键词/短句，提升检索召回。
        保留核心概念（如黄金、期货、价格），弱化过于具体的日期/数字，输出简短、利于语义匹配的检索词。
        """
        system_prompt = """你是一个检索查询改写助手。任务：把用户的自然语言问题改写成适合向量检索的「关键词/短句」形式。

要求：
1. 理解用户想问的核心问题是什么，并用更精确专业的词汇描述出来。
2. 挑选出核心概念和主题词（如：期货现货的价格 -> 收盘价/开盘价/最新价）。
3. 明确具体的约束，但改为新闻标题友好的描述方式。
4. 输出 1～2 句简短检索词，用中文，不要完整问句，不要解释。
5. 只输出改写后的检索词，不要其他内容。

【重要】禁止使用以下抽象/口语化词汇，必须替换为新闻常用词：
- 最新动态 / 相关情况 / 有关信息 / 怎么样了 -> 资讯 / 新闻 / 报道 / 消息
- 发生了什么 / 什么情况 -> 事件 / 进展 / 动向
- 近期表现 / 最近如何 -> 行情 / 走势 / 涨跌

示例：
- 用户：「体育最新动态」-> 输出：「体育赛事新闻 体育资讯」
- 用户：「黄金怎么样了」-> 输出：「黄金行情 金价走势」
- 用户：「中美关系有什么情况」-> 输出：「中美关系动向 中美外交新闻」"""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_query},
        ]
        try:
            rewritten = self.chat(messages)
            rewritten = (rewritten or "").strip()
            if rewritten:
                return rewritten
        except Exception as e:
            logger.warning(f"查询改写失败，使用原问题检索: {e}")
        return user_query

    def expand_queries_for_search(self, user_query: str, num_variants: int = 3) -> List[str]:
        """
        生成多个检索查询变体，提升召回覆盖率。
        
        Args:
            user_query: 用户原始问题
            num_variants: 生成变体数量（默认3个）
            
        Returns:
            检索查询变体列表
        """
        system_prompt = f"""你是一个检索查询扩展助手。任务：为用户问题生成 {num_variants} 个不同表述的检索关键词，用于向量检索。

要求：
1. 每个变体使用不同的同义词或表述方式，覆盖更多可能的新闻标题/内容匹配
2. 使用新闻报道中常见的词汇（资讯/新闻/报道/行情/走势/动向等）
3. 避免抽象口语词（最新动态/相关情况/怎么样了）
4. 每行输出一个变体，共 {num_variants} 行，不要编号，不要解释

示例输入：「体育最近怎么样」
示例输出：
体育赛事新闻
体育资讯报道
体育比赛动态"""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_query},
        ]
        try:
            result = self.chat(messages)
            if result:
                variants = [line.strip() for line in result.strip().split("\n") if line.strip()]
                if variants:
                    logger.debug(f"查询扩展: '{user_query}' -> {variants}")
                    return variants[:num_variants]
        except Exception as e:
            logger.warning(f"查询扩展失败: {e}")
        # fallback: 使用单次改写
        return [self.rewrite_query_for_search(user_query)]

    def generate_answer(
        self,
        query: str,
        context: List[Dict],
        system_prompt: Optional[str] = None
    ) -> str:
        """
        基于上下文生成回答
        
        Args:
            query: 用户问题
            context: 检索到的上下文（包含title, content, source, link等）
            system_prompt: 系统提示词
            
        Returns:
            AI回答
        """
        if system_prompt is None:
            system_prompt = self._build_news_system_prompt()
        
        # 构建上下文文本（带序号，供来源引用）
        context_text = "\n\n".join(
            f"{i}. {self._format_news_item(item)}" for i, item in enumerate(context, 1)
        )
        user_content = self._build_news_user_prompt(context_text, query)
        
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content}
        ]
        answer = self.chat(messages)
        # 1. 捏造检测
        if not self._verify_answer(query, answer, context):
            logger.info("回答未通过事实核查，返回保守回复。被过滤的回答：{}", answer)
            return "根据当前检索到的内容无法可靠回答该问题，请稍后重试或换个问法。"
        # 2. 日期修正（仅在检测到模糊日期时调用）
        answer = self._fix_date_formatting(answer, context)
        return answer

    def generate_answer_stream(
        self,
        query: str,
        context: List[Dict],
        system_prompt: Optional[str] = None,
        deep_think: bool = False
    ) -> Iterator[Dict]:
        """
        基于上下文流式生成回答，yield 的每个事件为可 JSON 序列化的 dict：
        - 内容块: {"choices": [{"delta": {"content": "..."}}]}
        - 校验失败替换: {"replace": "根据当前检索..."}
        - 结束: {"sources": [...], "done": True}（由调用方注入 sources）
        """
        if system_prompt is None:
            system_prompt = self._build_news_system_prompt()

        context_text = "\n\n".join(
            f"{i}. {self._format_news_item(item)}" for i, item in enumerate(context, 1)
        )
        original_user_text = self._build_news_user_prompt(context_text, query)
        #《Prompt Repetition Improves Non-Reasoning LLMs》 (arXiv:2512.14982)
        # 没想到吧, 真正的trick就是这么朴实无华
        user_content = f"{original_user_text}\n\nRead again:\n{original_user_text}" 
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content}
        ]
        accumulated = []
        for chunk in self.chat_stream(messages, deep_think=deep_think):
            accumulated.append(chunk)
            yield {"choices": [{"delta": {"content": chunk}}]}
        full_answer = "".join(accumulated)
        # 1. 捏造检测
        if not self._verify_answer(query, full_answer, context):
            logger.info("流式回答未通过事实核查，返回保守回复。被过滤的回答：{}", full_answer)
            yield {"replace": "根据当前检索到的内容无法可靠回答该问题，请稍后重试或换个问法。"}
            return
        # 2. 日期修正（仅在检测到模糊日期时调用）
        fixed = self._fix_date_formatting(full_answer, context)
        if fixed != full_answer:
            logger.info("流式回答日期已修正")
            yield {"replace": fixed}

    def close(self):
        """关闭HTTP客户端"""
        self.client.close()
