# scripts/test_all_api.py
"""
后端 API 全量测试脚本。
用于排查 chat 失败、数据库/向量库等问题；可单独测试每个接口。
需在 miniprogram-server 项目根目录执行：python scripts/test_all_api.py

环境变量（.env 或当前 shell）：
  FLASK_HOST, FLASK_PORT  - 脚本请求的目标地址（本机测本机时务必设 FLASK_HOST=localhost 避免 192.168.0.89 读超时）
  JWT_TOKEN               - 有效 JWT，用于需登录的接口（推荐先小程序登录后从 storage 复制）
  TEST_WECHAT_CODE        - 可选，用于尝试 POST /api/auth/login 获取 token
"""
import os
import sys
import time
import json

# 保证能加载到项目根目录的 .env
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.chdir(_ROOT)

from dotenv import load_dotenv

_env_path = os.path.join(_ROOT, ".env")
_load = load_dotenv(_env_path)

try:
    import requests
except ImportError:
    print("请安装 requests: pip install requests")
    sys.exit(1)

# 从 .env 读取（0.0.0.0 是服务端监听用，客户端不能连 0.0.0.0，本机测时改为 localhost）
FLASK_HOST_RAW = os.getenv("FLASK_HOST", "localhost")
FLASK_HOST = "localhost" if FLASK_HOST_RAW == "0.0.0.0" else FLASK_HOST_RAW
FLASK_PORT = os.getenv("FLASK_PORT", "8082")
BASE_URL = f"http://{FLASK_HOST}:{FLASK_PORT}"
JWT_TOKEN = os.getenv("JWT_TOKEN", "").strip()
TEST_WECHAT_CODE = os.getenv("TEST_WECHAT_CODE", "").strip()

# 超时（秒）：普通接口 20，RAG/chat 60
TIMEOUT = 5
TIMEOUT_LONG = 15


def headers(with_auth=True):
    h = {"Content-Type": "application/json"}
    if with_auth and JWT_TOKEN:
        h["Authorization"] = f"Bearer {JWT_TOKEN}"
    return h


def ok(res) -> bool:
    if res.status_code not in (200, 201):
        return False
    try:
        data = res.json()
        return data.get("success", True) if isinstance(data, dict) else True
    except Exception:
        return res.status_code in (200, 201)


def run(name: str, method: str, path: str, json_body=None, timeout=TIMEOUT, auth=True, stream=False):
    url = BASE_URL + path
    t0 = time.perf_counter()
    try:
        if stream:
            r = requests.request(
                method, url, json=json_body, headers=headers(auth),
                timeout=timeout, stream=True
            )
            # 只读前几行以确认流式正常，不读完
            chunks = []
            for i, line in enumerate(r.iter_lines(decode_unicode=True)):
                if i >= 5:
                    break
                if line and line.startswith("data:"):
                    chunks.append(line)
            elapsed = time.perf_counter() - t0
            success = r.status_code == 200 and len(chunks) >= 0
            return success, r.status_code, elapsed, (f"stream chunks: {len(chunks)}" if chunks else "ok")
        else:
            r = requests.request(
                method, url, json=json_body, headers=headers(auth),
                timeout=timeout
            )
    except requests.exceptions.Timeout as e:
        elapsed = time.perf_counter() - t0
        return False, 0, elapsed, f"timeout: {e}"
    except requests.exceptions.RequestException as e:
        elapsed = time.perf_counter() - t0
        return False, 0, elapsed, str(e)
    elapsed = time.perf_counter() - t0
    try:
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text
    except Exception:
        body = r.text[:200]
    if not ok(r):
        return False, r.status_code, elapsed, body
    return True, r.status_code, elapsed, body


def main():
    print("--- 环境 ---")
    print(f"  项目根: {_ROOT}")
    print(f"  .env 路径: {_env_path}  存在: {os.path.isfile(_env_path)}  已加载: {_load}")
    print(f"  FLASK_HOST(原始): {FLASK_HOST_RAW!r}  -> 请求用: {FLASK_HOST!r}")
    print(f"  FLASK_PORT: {FLASK_PORT!r}")
    print(f"  BASE_URL: {BASE_URL}")
    print(f"  JWT_TOKEN: {'(已设置)' if JWT_TOKEN else '(未设置)'}")
    print(f"  TEST_WECHAT_CODE: {'(已设置)' if TEST_WECHAT_CODE else '(未设置)'}")
    print("---")
    print("若出现 404 且 body 为 {\"detail\": \"Not Found\"}，多半是 8081 被其它进程占用，并非 miniprogram-server。请确认 run.py 在本机占用的端口（或改 .env FLASK_PORT 再重启）。")
    print("-" * 60)

    cases = []

    def add(name, *run_args, **run_kw):
        r = run(name, *run_args, **run_kw)
        cases.append((name, r[0], r[1], r[2], r[3]))

    # 0. 先打一个无需鉴权、最轻的接口，确认服务可达（避免第一个就打到需鉴权或冷启动慢的接口）
    add("GET /api/rag/info", "GET", "/api/rag/info", auth=False)

    # 1. 登录（可选，依赖 TEST_WECHAT_CODE）
    if TEST_WECHAT_CODE:
        success, code, elapsed, msg = run(
            "login", "POST", "/api/auth/login",
            json_body={"code": TEST_WECHAT_CODE},
            auth=False
        )
        cases.append(("POST /api/auth/login", success, code, elapsed, msg))
        if success and isinstance(msg, dict) and msg.get("data") and isinstance(msg["data"], dict):
            token = msg["data"].get("token")
            if token:
                print(f"  [提示] 从登录响应取得 token，后续请求可设置 JWT_TOKEN={token[:20]}...")
    else:
        cases.append(("POST /api/auth/login", None, None, None, "skip (无 TEST_WECHAT_CODE)"))

    # 2. 用户与同步（需 JWT）
    add("GET /api/user/info", "GET", "/api/user/info", auth=True)
    add("GET /api/sync/user_profile/latest", "GET", "/api/sync/user_profile/latest", auth=True)
    add("GET /api/sync/agent_settings/latest", "GET", "/api/sync/agent_settings/latest", auth=True)
    add(
        "GET /api/sync/conversations/latest",
        "GET", "/api/sync/conversations/latest?messagesPerConversation=0",
        auth=True
    )

    # 3. 对话（需 JWT）
    chat_id = "test_chat_" + str(int(time.time()))
    add("GET /api/conversations/<id>/messages", "GET", f"/api/conversations/{chat_id}/messages?limit=5", auth=True)
    add(
        "POST /api/conversations/<id>/messages",
        "POST", f"/api/conversations/{chat_id}/messages",
        json_body={
            "userContent": "脚本测试用户消息",
            "agentContent": "脚本测试助手回复",
            "title": "API 测试对话",
        },
        auth=True
    )

    # 4. RAG：意图 + 查询 + 信息（意图/查询需 JWT；info 无需）
    add(
        "POST /api/rag/intent",
        "POST", "/api/rag/intent",
        json_body={"query": "今天黄金价格多少", "history_turns": 0},
        timeout=TIMEOUT_LONG,
        auth=True
    )
    add(
        "POST /api/rag/query",
        "POST", "/api/rag/query",
        json_body={"query": "今天黄金价格", "top_k": 2, "history_turns": 0},
        timeout=TIMEOUT_LONG,
        auth=True
    )
    add("GET /api/rag/pipeline/stats", "GET", "/api/rag/pipeline/stats?hours=1", auth=True)

    # 5. 统一对话入口（流式，易超时）
    add(
        "POST /api/chat (stream)",
        "POST", "/api/chat",
        json_body={"query": "你好", "history_turns": 0},
        timeout=TIMEOUT_LONG,
        auth=True,
        stream=True
    )

    # 输出汇总
    print()
    for item in cases:
        name, success, code, elapsed, msg = item[0], item[1], item[2], item[3], item[4]
        if success is None:
            status = "SKIP"
        else:
            status = "OK" if success else "FAIL"
        code_str = str(code) if code else "-"
        elapsed_str = f"{elapsed:.2f}s" if elapsed is not None else "-"
        msg_str = str(msg)
        if isinstance(msg, (dict, list)):
            msg_str = json.dumps(msg, ensure_ascii=False)[:120]
        if len(msg_str) > 100:
            msg_str = msg_str[:97] + "..."
        print(f"  [{status}] {name}  -> {code_str}  {elapsed_str}  {msg_str}")
    print("-" * 60)
    passed = sum(1 for c in cases if len(c) == 5 and c[1] is True)
    total = sum(1 for c in cases if len(c) == 5 and c[1] is not None)
    print(f"通过: {passed}/{total}（未设置 JWT_TOKEN 时部分接口 401 属预期）")


if __name__ == "__main__":
    main()
