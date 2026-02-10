# app/services/local_llm_service.py
"""
本地LLM服务 - 通过 HTTP 调用 WSL vLLM OpenAI 兼容 API
用于 QueryRewriter 和 IntentClassifier 等低延迟任务

架构: Windows Flask 后端 -> HTTP -> WSL vLLM 服务
"""
import os
import json
from typing import List, Dict, Type, Optional
from pydantic import BaseModel
from loguru import logger

import httpx


class LocalLLMService:
    """
    本地LLM服务，通过 HTTP 调用 vLLM OpenAI 兼容 API
    
    vLLM 服务运行在 WSL 中，提供 OpenAI 兼容的 API:
    - POST /v1/chat/completions (对话补全)
    - GET /v1/models (模型列表)
    - GET /health (健康检查)
    """
    _instance: Optional['LocalLLMService'] = None
    _client: Optional[httpx.Client] = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        if LocalLLMService._client is not None:
            return
        
        # 从Flask配置或环境变量获取参数
        try:
            from flask import current_app
            self.api_base = current_app.config.get('LOCAL_LLM_API_BASE', 'http://localhost:8001/v1')
            self.api_key = current_app.config.get('LOCAL_LLM_API_KEY', '')
            self.model = current_app.config.get('LOCAL_LLM_MODEL', 'qwen2.5-3b-instruct-awq')
            self.timeout = current_app.config.get('LOCAL_LLM_TIMEOUT', 30)
        except RuntimeError:
            # 非Flask上下文时使用环境变量
            self.api_base = os.getenv('LOCAL_LLM_API_BASE', 'http://localhost:8001/v1')
            self.api_key = os.getenv('LOCAL_LLM_API_KEY', '')
            self.model = os.getenv('LOCAL_LLM_MODEL', 'qwen2.5-3b-instruct-awq')
            self.timeout = int(os.getenv('LOCAL_LLM_TIMEOUT', '30'))
        
        self._init_client()
    
    def _init_client(self):
        """初始化 HTTP 客户端"""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        
        LocalLLMService._client = httpx.Client(
            timeout=httpx.Timeout(self.timeout, connect=5.0),
            headers=headers
        )
        logger.info(f"本地LLM服务已初始化: {self.api_base}, 模型: {self.model}")
    
    @property
    def is_available(self) -> bool:
        """
        检查 vLLM 服务是否可用
        通过调用 /v1/models 端点验证
        """
        try:
            resp = LocalLLMService._client.get(
                f"{self.api_base}/models",
                timeout=2.0
            )
            return resp.status_code == 200
        except Exception as e:
            logger.debug(f"vLLM 服务不可用: {e}")
            return False
    
    def health_check(self) -> Dict:
        """
        健康检查，返回服务状态详情
        
        Returns:
            包含 available, model, latency_ms 等信息的字典
        """
        import time
        result = {
            "available": False,
            "api_base": self.api_base,
            "model": self.model,
            "latency_ms": None,
            "error": None
        }
        
        try:
            start = time.perf_counter()
            resp = LocalLLMService._client.get(
                f"{self.api_base}/models",
                timeout=5.0
            )
            latency = (time.perf_counter() - start) * 1000
            
            if resp.status_code == 200:
                result["available"] = True
                result["latency_ms"] = round(latency, 2)
                models_data = resp.json()
                result["models"] = [m["id"] for m in models_data.get("data", [])]
            else:
                result["error"] = f"HTTP {resp.status_code}"
        except httpx.TimeoutException:
            result["error"] = "连接超时"
        except httpx.ConnectError:
            result["error"] = "无法连接到 vLLM 服务"
        except Exception as e:
            result["error"] = str(e)
        
        return result
    
    def chat(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.1,
        max_tokens: int = 768
    ) -> str:
        """
        基础对话接口
        
        Args:
            messages: 消息列表，格式: [{"role": "user", "content": "..."}]
            temperature: 温度参数（分类任务建议低温度）
            max_tokens: 最大生成token数
            
        Returns:
            模型输出文本
            
        Raises:
            RuntimeError: 当服务不可用或请求失败时
        """
        try:
            response = LocalLLMService._client.post(
                f"{self.api_base}/chat/completions",
                json={
                    "model": self.model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens
                }
            )
            response.raise_for_status()
            
            data = response.json()
            content = data["choices"][0]["message"]["content"]
            return content.strip()
            
        except httpx.TimeoutException:
            logger.error("vLLM 服务请求超时")
            raise RuntimeError("本地LLM服务请求超时")
        except httpx.ConnectError:
            logger.error("无法连接到 vLLM 服务")
            raise RuntimeError("无法连接到本地LLM服务")
        except httpx.HTTPStatusError as e:
            try:
                body = e.response.text or e.response.content.decode("utf-8", errors="replace")
            except Exception:
                body = str(e.response.content)
            body_trim = body[:2000] if len(body) > 2000 else body
            logger.error(
                "vLLM 服务返回错误: HTTP {} | body: {}",
                e.response.status_code,
                body_trim,
            )
            raise RuntimeError(f"本地LLM服务错误: HTTP {e.response.status_code}")
        except Exception as e:
            logger.error(f"本地模型推理失败: {e}")
            raise RuntimeError(f"本地LLM服务错误: {e}")
    
    def chat_with_schema(
        self,
        messages: List[Dict[str, str]],
        response_schema: Type[BaseModel],
        temperature: float = 0.1,
        max_tokens: int = 768
    ) -> BaseModel:
        """
        结构化输出接口，通过 prompt 工程 + JSON 解析实现        
        Args:
            messages: 消息列表
            response_schema: Pydantic 模型类，定义输出结构
            temperature: 温度参数
            max_tokens: 最大生成token数
            
        Returns:
            解析后的 Pydantic 模型实例
            
        Raises:
            RuntimeError: 当服务不可用或请求失败时
        """
        try:
            # FastChat 不支持 guided_json，使用常规 chat 接口
            response = LocalLLMService._client.post(
                f"{self.api_base}/chat/completions",
                json={
                    "model": self.model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens
                }
            )
            response.raise_for_status()
            
            data = response.json()
            content = data["choices"][0]["message"]["content"].strip()
            
            # 清理 markdown 代码块
            if "```" in content:
                start = content.find("```")
                rest = content[start + 3:]
                if rest.startswith("json"):
                    rest = rest[4:].lstrip()
                end = rest.find("```")
                content = rest[:end].strip() if end >= 0 else rest.strip()
            
            # 尝试从文本中提取 JSON
            if not content.startswith("{"):
                start = content.find("{")
                end = content.rfind("}") + 1
                if start >= 0 and end > start:
                    content = content[start:end]
            
            # 解析并验证 JSON
            return response_schema.model_validate_json(content)
            
        except httpx.TimeoutException:
            logger.error("vLLM 服务请求超时")
            raise RuntimeError("本地LLM服务请求超时")
        except httpx.ConnectError:
            logger.error("无法连接到 vLLM 服务")
            raise RuntimeError("无法连接到本地LLM服务")
        except httpx.HTTPStatusError as e:
            try:
                body = e.response.text or e.response.content.decode("utf-8", errors="replace")
            except Exception:
                body = str(e.response.content)
            body_trim = body[:2000] if len(body) > 2000 else body
            logger.error(
                "vLLM 服务返回错误: HTTP {} | body: {}",
                e.response.status_code,
                body_trim,
            )
            raise RuntimeError(f"本地LLM服务错误: HTTP {e.response.status_code}")
        except json.JSONDecodeError as e:
            logger.error(f"JSON 解析失败: {e}")
            raise RuntimeError(f"结构化输出解析失败: {e}")
        except Exception as e:
            logger.error(f"结构化输出失败: {e}")
            raise RuntimeError(f"结构化输出失败: {e}")
    
    def chat_json(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.1,
        max_tokens: int = 768
    ) -> dict:
        """
        JSON输出接口（无Schema约束，仅要求JSON格式）
        Args:
            messages: 消息列表
            temperature: 温度参数
            max_tokens: 最大生成token数
            
        Returns:
            解析后的字典
            
        Raises:
            RuntimeError: 当服务不可用或请求失败时
        """
        try:
            # FastChat 不支持 response_format，使用常规 chat 接口
            response = LocalLLMService._client.post(
                f"{self.api_base}/chat/completions",
                json={
                    "model": self.model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens
                }
            )
            response.raise_for_status()
            
            data = response.json()
            content = data["choices"][0]["message"]["content"].strip()
            
            # 清理 markdown 代码块
            if "```" in content:
                start = content.find("```")
                rest = content[start + 3:]
                if rest.startswith("json"):
                    rest = rest[4:].lstrip()
                end = rest.find("```")
                content = rest[:end].strip() if end >= 0 else rest.strip()
            
            # 尝试从文本中提取 JSON
            if not content.startswith("{") and not content.startswith("["):
                start = content.find("{")
                if start < 0:
                    start = content.find("[")
                end = max(content.rfind("}"), content.rfind("]")) + 1
                if start >= 0 and end > start:
                    content = content[start:end]
            
            return json.loads(content)
            
        except httpx.TimeoutException:
            logger.error("vLLM 服务请求超时")
            raise RuntimeError("本地LLM服务请求超时")
        except httpx.ConnectError:
            logger.error("无法连接到 vLLM 服务")
            raise RuntimeError("无法连接到本地LLM服务")
        except httpx.HTTPStatusError as e:
            try:
                body = e.response.text or e.response.content.decode("utf-8", errors="replace")
            except Exception:
                body = str(e.response.content)
            body_trim = body[:2000] if len(body) > 2000 else body
            logger.error(
                "vLLM 服务返回错误: HTTP {} | body: {}",
                e.response.status_code,
                body_trim,
            )
            raise RuntimeError(f"本地LLM服务错误: HTTP {e.response.status_code}")
        except json.JSONDecodeError as e:
            logger.error(f"JSON 解析失败: {e}")
            raise RuntimeError(f"JSON输出解析失败: {e}")
        except Exception as e:
            logger.error(f"JSON输出失败: {e}")
            raise RuntimeError(f"JSON输出失败: {e}")
    
    def close(self):
        """关闭 HTTP 客户端"""
        if LocalLLMService._client:
            LocalLLMService._client.close()
            LocalLLMService._client = None
            logger.info("本地LLM服务已关闭")


# 全局实例获取函数
_local_llm_instance: Optional[LocalLLMService] = None


def get_local_llm_service() -> LocalLLMService:
    """获取本地LLM服务单例"""
    global _local_llm_instance
    if _local_llm_instance is None:
        _local_llm_instance = LocalLLMService()
    return _local_llm_instance
