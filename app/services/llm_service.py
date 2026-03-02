# app/services/llm_service.py
"""
LLM服务 - GLM-4-Flash API调用
支持同步与流式（SSE）对话
"""
import json
import re
from datetime import datetime
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
    
    def __init__(self, local_llm_service=None):
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
        self._local_llm = local_llm_service
    
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
- **报道时间**（published_time）：每条新闻的「报道发布时间」字段，表示稿件发布时间，仅供内部校验和时效排序，**不得直接作为事件发生日期告诉用户**。
- **事件时间**（event_time）：新闻描述的事实发生日期，从正文提取；赛况数据引擎中的 date 字段为事件时间。用户问「昨天/前天」时，应以**事件时间**为准作答；赛况数据引擎有对应日期数据时优先使用。

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
- 若提供的检索结果中包含「来源：赛况数据引擎」的比分数据，且其中已有用户所问主体（如某球队）的比赛/比分信息，则仅根据该条作答即可，不得再追加与首条并列的「未检索到…」「未检索到关于XX其他…」等说明；仅在整份检索结果中均无与用户所问条件直接相关的内容时，才输出一句规则 5 规定的「未检索到相关信息」；
- **赛况优先**：赛况数据引擎的比分优先于检索片段；若 context 里已有「来源：赛况数据引擎」的比分，禁止用检索到的新闻内容编造或改写比分数字，仅可转述赛况数据中的内容；
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
    def _build_news_user_prompt(
        context_text: str,
        query: str,
        coverage_note: str = "",
        original_query: Optional[str] = None,
        rewrite_reasoning: Optional[str] = None,
        detail_follow_up: bool = False,
        reference_date: Optional[str] = None,
        current_date: Optional[str] = None,
    ) -> str:
        """构建新闻回答的 user prompt（含回答要求 + 检索覆盖情况 + 检索内容 + 用户问题）。reference_date 为回答范围日期（answer_scope_date），有值时才注入「目标日期」约束。"""
        instruction = LLMService._build_news_answer_instruction()
        coverage_block = f"\n{coverage_note}\n" if coverage_note else ""
        grounding_block = ""
        if original_query and original_query.strip() != query.strip():
            grounding_block = (
                f"你当前看到的参考资料是基于对原问题「{original_query}」的扩展搜索得来的，"
                "请确保回答不偏离原问题核心。\n\n"
            )
        if rewrite_reasoning and rewrite_reasoning.strip():
            grounding_block += f"改写说明：{rewrite_reasoning}\n\n"
        if detail_follow_up:
            grounding_block += (
                "用户当前为追问细节，请完整呈现检索内容中的比分、节次与球员数据，不要概括压缩；"
                "新闻稿可依长度适度概括或原样输出。\n\n"
            )
        date_constraint = ""
        if reference_date and current_date:
            try:
                ref_dt = datetime.strptime(reference_date.strip()[:10], "%Y-%m-%d")
                ref_ymd = f"{ref_dt.year}年{ref_dt.month}月{ref_dt.day}日"
                date_constraint = (
                    "## 日期约束（最高优先级）\n"
                    f"本问题询问的是「刚结束/今天」等时间范围，目标日期为 **{ref_ymd}**。\n"
                    "- 你**仅可播报事件发生日期等于该日的新闻**。正文中的「报道发布时间」不是事件发生日期；事件发生日期以正文内「X月X日讯」「X月X日」等为准。\n"
                    "- 播报时**必须在回答中明确写出事件日期**（如「YYYY年M月D日」）；若目标日即为今日，可写「今天」且仅当该事件确为今日发生时。\n"
                    "- **禁止**用「刚刚」「刚结束」等模糊词指代非当日的旧闻；若检索结果中无该日期的比赛/事件，应明确说明「该日暂无」或「目前没有该日已结束的场次」等。\n"
                )
                if "进行中" in context_text:
                    date_constraint += "- 若检索内容中某场比赛标注为「进行中」，则回答中对该场**仅可写当前比分与领先方**，**不得**使用「获胜」「击败」「取胜」「险胜」等表示已结束的措辞。\n"
                date_constraint += "\n"
            except ValueError:
                pass
        return f"""请严格遵循以下回答要求：

{instruction}
{date_constraint}{grounding_block}---
{coverage_block}
以下是系统根据用户问题从新闻数据库中检索到的内容：

{context_text}

---

用户的问题：{query}"""

    # 检测模糊日期的正则（用于决定是否需要调用日期修正）
    _VAGUE_DATE_RE = re.compile(
        r'据最新报道|据报道(?!发布)|近日[，,]|最近[，,]|据悉[，,]'
    )

    # ---- Markdown 加粗正则（纯规则，零延迟） ----
    # 价格/金额：数字 + 可选万/亿 + 货币/重量单位 + 可选子单位
    _MD_PRICE_RE = re.compile(
        r'(\d[\d,]*(?:\.\d+)?\s*(?:万|亿)?\s*'
        r'(?:美元|元|港元|日元|欧元|英镑|美分|吨|盎司|克|千克|桶|股)'
        r'(?:/[^\s，。、；\n【】]+)?)'
    )
    # 百分比/涨跌幅
    _MD_PCT_RE = re.compile(r'([+-±]?\d[\d,.]*%)')
    # 体育比分（排除时间格式 HH:MM:SS）
    _MD_SCORE_RE = re.compile(r'(?<![:\d])(\d{1,3}:\d{1,3})(?![:\d])')

    # ---- 时间证据与一致性（reference_date 明确时） ----
    # 回答中事件日期抽取：YYYY年M月D日、YYYY-M-D、M月D日等
    _ANSWER_DATE_RE = re.compile(
        r'(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日|(\d{4})-(\d{1,2})-(\d{1,2})'
    )

    @staticmethod
    def _normalize_reference_date(ref: str) -> Optional[str]:
        """将 reference_date 规范为 YYYY-MM-DD；无效则返回 None。"""
        if not ref or not isinstance(ref, str):
            return None
        ref = ref.strip()
        if len(ref) == 10 and ref[4] == "-" and ref[7] == "-":
            try:
                datetime.strptime(ref, "%Y-%m-%d")
                return ref
            except ValueError:
                pass
        # 尝试 M月D日 + 默认年
        m = re.match(r"(\d{1,2})月(\d{1,2})日", ref)
        if m:
            try:
                y = datetime.now().year
                dt = datetime(y, int(m.group(1)), int(m.group(2)))
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                pass
        return None

    @staticmethod
    def _ts_to_date_str(ts: float, fallback: Optional[str] = None) -> Optional[str]:
        """将 event_time_timestamp (float) 转为 YYYY-MM-DD"""
        try:
            dt = datetime.fromtimestamp(float(ts))
            return dt.strftime("%Y-%m-%d")
        except (ValueError, TypeError, OSError):
            return fallback

    @staticmethod
    def _parse_rel_date_in_text(text: str, current_date: str) -> Optional[str]:
        """从 rule_event_time 等文本解析「昨日」「昨天」等为 YYYY-MM-DD"""
        if not text or not current_date:
            return None
        lower = text.strip().lower()
        try:
            base = datetime.strptime(current_date, "%Y-%m-%d")
        except ValueError:
            return None
        from datetime import timedelta
        if "今日" in lower or "今天" in lower:
            return current_date
        if "昨日" in lower or "昨天" in lower:
            return (base - timedelta(days=1)).strftime("%Y-%m-%d")
        if "前天" in lower or "前日" in lower:
            return (base - timedelta(days=2)).strftime("%Y-%m-%d")
        return None

    @staticmethod
    def has_evidence_for_date(
        context: List[Dict],
        reference_date: Optional[str],
        current_date: Optional[str] = None,
    ) -> bool:
        """
        校验 context 中是否含有该日期的证据。
        优先用 rule_event_time、event_time_timestamp 标准化为 YYYY-MM-DD 比较；
        赛况数据引擎 content 含该日也返回 True。避免仅扫正文文本。
        """
        if not reference_date or not context:
            return True
        canon = LLMService._normalize_reference_date(reference_date)
        if not canon:
            return True
        variants = []
        try:
            dt = datetime.strptime(canon, "%Y-%m-%d")
            y, m, d = dt.year, dt.month, dt.day
            variants = [canon, f"{y}年{m}月{d}日", f"{m}月{d}日", f"{m}-{d}", f"{m}/{d}"]
        except ValueError:
            variants = [canon]

        for item in context:
            src = (item.get("source") or "").strip()

            # event_time_timestamp 标准化为 YYYY-MM-DD
            ets = item.get("event_time_timestamp")
            if ets is not None:
                item_date = LLMService._ts_to_date_str(ets)
                if item_date == canon:
                    return True

            # rule_event_time 标准化或解析
            ret = item.get("rule_event_time")
            if ret:
                ret_str = (ret if isinstance(ret, str) else str(ret)).strip()
                if ret_str.startswith(canon) or canon in ret_str:
                    return True
                for v in variants:
                    if v in ret_str:
                        return True
                parsed = LLMService._parse_rel_date_in_text(ret_str, current_date or "")
                if parsed == canon:
                    return True

            # 赛况数据引擎：content 中日期行
            if src == "赛况数据引擎":
                content = item.get("content") or ""
                for line in content.split("\n"):
                    for v in variants:
                        if v in line:
                            return True
        return False

    def _format_to_markdown(self, answer: str) -> str:
        """
        纯文本 → Markdown 格式化（纯正则，零延迟，无截断风险）。
        对价格/金额、百分比/涨跌幅、体育比分添加 **加粗**。
        """
        if not answer:
            return answer
        text = answer
        text = self._MD_PRICE_RE.sub(r'**\1**', text)
        text = self._MD_PCT_RE.sub(r'**\1**', text)
        text = self._MD_SCORE_RE.sub(r'**\1**', text)
        return text

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

    def decompose_query(self, standalone_query: str) -> List[str]:
        """
        将包含多个独立检索意图的查询分解为子查询。

        单一意图（"黄金行情"）→ ["黄金行情"]
        多意图  （"伦敦金和COMEX指标"）→ ["伦敦金指标", "COMEX指标"]

        仅当查询包含并列连词时才调用 LLM 判断，否则直接返回原查询。
        """
        # 快速排除：不含并列连词 → 无需分解
        if not any(conj in standalone_query for conj in ('和', '与', '以及', '、', '跟', '还有')):
            return [standalone_query]

        system_prompt = """你是查询分解助手。判断查询是否包含多个需要分别检索的独立主体，如果是则拆分。

规则：
1. "A和B"、"A、B"等并列结构中，A和B是不同检索主体时 → 拆分为独立子查询
2. 复合概念不拆分："中美关系"是一个主题不拆；"进出口数据"不拆
3. 每个子查询必须完整可独立检索（保留共享修饰词，如"行情""指标""最新"）
4. 每行输出一个子查询，不编号不解释
5. 不需要拆分时原样输出一行"""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": standalone_query},
        ]
        try:
            result = self.chat(messages, temperature=0.1, max_tokens=200)
            if result:
                sub_queries = [
                    line.strip() for line in result.strip().split("\n") if line.strip()
                ]
                if sub_queries:
                    if len(sub_queries) > 1:
                        logger.info(
                            f"查询分解: '{standalone_query}' -> {sub_queries}"
                        )
                    return sub_queries
        except Exception as e:
            logger.warning(f"查询分解失败: {e}")
        return [standalone_query]

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

    def expand_queries_for_search(
        self,
        user_query: str,
        num_variants: int = 3,
        reference_date: Optional[str] = None,
        current_date: Optional[str] = None,
    ) -> List[str]:
        """
        生成多个检索查询变体，提升召回覆盖率。

        reference_date：检索变体可选锚点日期（来自 temporal_context.reference_date），
        用于在变体中注入日期词以辅助召回；与「回答范围日期 answer_scope_date」无关。
        """
        base_prompt = f"""你是一个检索查询扩展助手。任务：为用户问题生成 {num_variants} 个不同表述的检索关键词，用于向量检索。

要求：
1. 每个变体使用不同的同义词或表述方式，覆盖更多可能的新闻标题/内容匹配
2. 使用新闻报道中常见的词汇（资讯/新闻/报道/行情/走势/动向等）
3. 避免抽象口语词（最新动态/相关情况/怎么样了）
4. 每行输出一个变体，共 {num_variants} 行，不要编号，不要解释"""
        if reference_date and current_date:
            base_prompt += f"""

今日日期：{current_date}
用户询问的目标日期：{reference_date}（YYYY-MM-DD）。若查询涉及某日（如昨天/前天），检索变体中应包含该日期的明确表述，例如：{reference_date[:4]}年{reference_date[5:7]}月{reference_date[8:10]}日马刺比赛、{reference_date[5:7]}-{reference_date[8:10]}NBA赛果。"""
        base_prompt += """

示例输入：「体育最近怎么样」
示例输出：
体育赛事新闻
体育资讯报道
体育比赛动态"""
        system_prompt = base_prompt
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
        system_prompt: Optional[str] = None,
        coverage_note: str = "",
        original_query: Optional[str] = None,
        rewrite_reasoning: Optional[str] = None,
        reference_date: Optional[str] = None,
        current_date: Optional[str] = None,
        detail_follow_up: bool = False,
    ) -> str:
        """
        基于上下文生成回答

        Args:
            query: 用户问题（改写后的独立查询）
            context: 检索到的上下文（包含title, content, source, link等）
            system_prompt: 系统提示词
            coverage_note: 检索覆盖情况说明（多意图查询有缺失时由 pipeline 注入）
            original_query: 用户原始问题（用于 Grounding Prompt）
            rewrite_reasoning: 改写说明（若有）
            reference_date: 回答范围日期（YYYY-MM-DD），由 pipeline 从 compute_answer_scope_date 传入；有则注入日期约束并做时间证据校验
            current_date: 今日日期（YYYY-MM-DD）；用于 has_evidence / _verify_time_consistency 解析「昨日」等
            detail_follow_up: 是否为细节追问；是则要求完整呈现比分/节次/球员，新闻稿可适度概括或原样
        Returns:
            AI回答
        """
        if system_prompt is None:
            system_prompt = self._build_news_system_prompt()

        if reference_date:
            if not self.has_evidence_for_date(context, reference_date, current_date):
                logger.info("时间证据校验: 检索结果中无目标日期证据，拒答。reference_date=%s", reference_date)
                return "当前检索结果中未找到该日期的报道，无法据此作答。"

        # 按报道时间从新到旧排序，保证正文序号与来源序号一致；无日期的排到末尾
        context = sorted(
            context,
            key=lambda item: item.get("published_time") or "",
            reverse=True,
        )

        # 构建上下文文本（带序号，供来源引用）
        context_text = "\n\n".join(
            f"{i}. {self._format_news_item(item)}" for i, item in enumerate(context, 1)
        )
        user_content = self._build_news_user_prompt(
            context_text, query, coverage_note,
            original_query=original_query,
            rewrite_reasoning=rewrite_reasoning,
            detail_follow_up=detail_follow_up,
            reference_date=reference_date,
            current_date=current_date,
        )
        
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content}
        ]
        answer = self.chat(messages)
        return answer

    def generate_answer_stream(
        self,
        query: str,
        context: List[Dict],
        system_prompt: Optional[str] = None,
        deep_think: bool = False,
        coverage_note: str = "",
        original_query: Optional[str] = None,
        rewrite_reasoning: Optional[str] = None,
        reference_date: Optional[str] = None,
        current_date: Optional[str] = None,
        detail_follow_up: bool = False,
    ) -> Iterator[Dict]:
        """
        基于上下文流式生成回答，yield 的每个事件为可 JSON 序列化的 dict：
        - 内容块: {"choices": [{"delta": {"content": "..."}}]}
        - 仅当「时间证据」缺失时 yield {"replace": "当前检索结果中未找到该日期的报道..."} 并 return
        校验与替换由调用方（Pipeline）在收齐全文后执行。
        reference_date 为回答范围日期（answer_scope_date），由 pipeline 传入。
        """
        if system_prompt is None:
            system_prompt = self._build_news_system_prompt()

        if reference_date:
            if not self.has_evidence_for_date(context, reference_date, current_date):
                logger.info("时间证据校验: 检索结果中无目标日期证据，拒答。reference_date=%s", reference_date)
                yield {"replace": "当前检索结果中未找到该日期的报道，无法据此作答。"}
                return

        # 按报道时间从新到旧排序，与 generate_answer 一致
        context = sorted(
            context,
            key=lambda item: item.get("published_time") or "",
            reverse=True,
        )

        context_text = "\n\n".join(
            f"{i}. {self._format_news_item(item)}" for i, item in enumerate(context, 1)
        )
        original_user_text = self._build_news_user_prompt(
            context_text, query, coverage_note,
            original_query=original_query,
            rewrite_reasoning=rewrite_reasoning,
            detail_follow_up=detail_follow_up,
            reference_date=reference_date,
            current_date=current_date,
        )
        #《Prompt Repetition Improves Non-Reasoning LLMs》 (arXiv:2512.14982)
        # 完整重复 prompt：让第二遍的 token 可以 attend 到第一遍的所有上下文
        user_content = f"{original_user_text}\n\nRead again:\n{original_user_text}"
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content}
        ]
        accumulated = []
        for chunk in self.chat_stream(messages, deep_think=deep_think):
            accumulated.append(chunk)
            yield {"choices": [{"delta": {"content": chunk}}]}
        # 校验与后处理由 Pipeline 在收齐全文后执行

    def generate_no_result_reply(
        self,
        query: str,
        reference_date: Optional[str] = None,
        current_date: Optional[str] = None,
    ) -> str:
        """
        检索 0 条时由 agent 生成一句说明，不拼模板。
        query 为改写后的独立查询，reference_date 为回答范围日期（若有）。
        """
        system = "你是菠萝快讯助手。当检索无结果时，用一两句话简要说明未检索到相关信息；若用户问的是某日某队/某主体，请明确写出日期与主体（如队名），避免让人误以为把「昨天」等时间词当关键词搜。"
        date_info = ""
        if reference_date:
            try:
                ref_dt = datetime.strptime(reference_date.strip()[:10], "%Y-%m-%d")
                date_info = f"回答范围日期：{ref_dt.year}年{ref_dt.month}月{ref_dt.day}日。"
            except ValueError:
                date_info = f"回答范围日期：{reference_date}。"
        else:
            date_info = "未限定具体日期。"
        user = f"检索结果为空。用户问题（独立查询）：{query}。{date_info}请用一两句话说明未检索到与该问题相关的信息。"
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        return self.chat(messages, temperature=0.3, max_tokens=150)

    def generate_no_result_reply_stream(
        self,
        query: str,
        reference_date: Optional[str] = None,
        current_date: Optional[str] = None,
    ) -> Iterator[Dict]:
        """检索 0 条时由 agent 流式生成一句说明，yield 格式与 generate_answer_stream 一致。"""
        system = "你是菠萝快讯助手。当检索无结果时，用一两句话简要说明未检索到相关信息；若用户问的是某日某队/某主体，请明确写出日期与主体（如队名），避免让人误以为把「昨天」等时间词当关键词搜。"
        date_info = ""
        if reference_date:
            try:
                ref_dt = datetime.strptime(reference_date.strip()[:10], "%Y-%m-%d")
                date_info = f"回答范围日期：{ref_dt.year}年{ref_dt.month}月{ref_dt.day}日。"
            except ValueError:
                date_info = f"回答范围日期：{reference_date}。"
        else:
            date_info = "未限定具体日期。"
        user = f"检索结果为空。用户问题（独立查询）：{query}。{date_info}请用一两句话说明未检索到与该问题相关的信息。"
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        for chunk in self.chat_stream(messages, temperature=0.3, max_tokens=150):
            yield {"choices": [{"delta": {"content": chunk}}]}

    def post_process_answer(self, answer: str, context: List[Dict]) -> str:
        """对生成结果做日期规范修正与 Markdown 格式化（供 Pipeline 在通过校验后调用）。"""
        answer = self._fix_date_formatting(answer, context)
        return self._format_to_markdown(answer)

    def close(self):
        """关闭HTTP客户端"""
        self.client.close()
