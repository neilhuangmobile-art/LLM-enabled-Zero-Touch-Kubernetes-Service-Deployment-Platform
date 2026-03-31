"""
core/claude_client.py
Claude API 快速推論 + K8s 對話助手

依賴：
  pip install anthropic
  環境變數：ANTHROPIC_API_KEY=sk-ant-...

提供兩個功能：
  1. claude_parse_k8s(prompt)  — 毫秒級 K8s JSON 解析（取代本地 LLaMA）
  2. claude_chat(message, history) — 多輪 K8s 對話助手
"""
import os
import json
import re
from typing import Optional

from core.config import SYSTEM_PROMPT

CHAT_SYSTEM = (
    "You are an expert Kubernetes and cloud infrastructure assistant. "
    "Help users understand and manage K8s deployments, troubleshoot issues, "
    "and follow best practices. "
    "Reply in the same language as the user (Chinese or English). "
    "Be concise and practical. Use code blocks for commands and YAML."
)

_client = None


def _get_client():
    global _client
    if _client is not None:
        return _client
    try:
        import anthropic
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            return None
        _client = anthropic.Anthropic(api_key=api_key)
        return _client
    except ImportError:
        return None


def is_available() -> bool:
    """檢查 Claude API 是否可用（已安裝 anthropic 且有 API key）。"""
    return _get_client() is not None


def claude_parse_k8s(prompt_text: str) -> Optional[dict]:
    """
    用 Claude API 解析 K8s 部署指令，回傳 dict 或 None。
    速度遠快於本地 LLaMA（< 2 秒 vs 數分鐘）。
    """
    client = _get_client()
    if client is None:
        return None
    try:
        response = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=256,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt_text}],
            thinking={"type": "adaptive"},
        )
        text = ""
        for block in response.content:
            if hasattr(block, "text"):
                text += block.text
        result = _parse_json(text)
        if result and _validate(result):
            result.setdefault("image", "nginx:latest")
            result.setdefault("app_name", "auto-app")
            result["pods"] = int(result["pods"])
            if "port" in result:
                try:
                    p = int(result["port"])
                    result["port"] = p if 1 <= p <= 65535 else None
                    if result["port"] is None:
                        del result["port"]
                except (ValueError, TypeError):
                    del result["port"]
            if "memory" in result:
                if not re.match(r"^\d+(Mi|Gi|Ki|M|G)$", str(result["memory"])):
                    del result["memory"]
            return result
        return None
    except Exception as e:
        print(f"[Claude API] 解析失敗：{e}")
        return None


def claude_chat(message: str, history: list) -> str:
    """
    多輪 K8s 對話助手。
    history: list of {"role": "user"|"assistant", "content": "..."}
    回傳 AI 回覆文字。
    """
    client = _get_client()
    if client is None:
        return "Claude API 未設定。請在 .env 或環境變數中設定 ANTHROPIC_API_KEY=sk-ant-..."
    try:
        messages = list(history) + [{"role": "user", "content": message}]
        response = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=1024,
            system=CHAT_SYSTEM,
            messages=messages,
            thinking={"type": "adaptive"},
        )
        text = ""
        for block in response.content:
            if hasattr(block, "text"):
                text += block.text
        return text.strip()
    except Exception as e:
        return f"[錯誤] {e}"


def _parse_json(text: str) -> Optional[dict]:
    """從回應文字中提取第一個完整 JSON 物件。"""
    start = text.find("{")
    if start == -1:
        return None
    depth, in_string, escape = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                snippet = text[start:i + 1]
                fixed = re.sub(r':\s*null\b', ': "NULL"', snippet)
                try:
                    result = json.loads(fixed)
                    if isinstance(result, dict):
                        return {k: v for k, v in result.items() if v != "NULL"}
                except Exception:
                    pass
                return None
    return None


def _validate(result: dict) -> bool:
    if not result or "error" in result:
        return False
    try:
        return 1 <= int(result.get("pods", 0)) <= 100
    except (ValueError, TypeError):
        return False
