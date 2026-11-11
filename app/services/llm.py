"""大模型插口（可选，默认关闭）。

不配置 LLM_API_KEY 时系统全部走免费本地路径，本模块不会被调用；
配置后自动启用增强功能：难解析证书页兜底、OCR 提质、裸币识别。

走 OpenAI 兼容协议，换厂商只改 LLM_BASE_URL / LLM_MODEL：
- 智谱免费档（0 元）：https://open.bigmodel.cn/api/paas/v4 + glm-4v-flash
- 硅基流动免费模型：https://api.siliconflow.cn/v1
- 本地 Ollama：http://NAS_IP:11434/v1 + 本地模型名
"""
import base64
from typing import Optional

from openai import OpenAI

from ..config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL


def is_enabled() -> bool:
    """调用方必须先检查这个：未配置 Key 时所有功能走免费路径。"""
    return bool(LLM_API_KEY)


_client: Optional[OpenAI] = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)
    return _client


def chat(prompt: str, image_paths: Optional[list] = None, max_tokens: int = 2000) -> str:
    """对话/视觉识别统一入口。调用前必须先确认 is_enabled() 为 True。"""
    content = [{"type": "text", "text": prompt}]
    for p in image_paths or []:
        with open(p, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
        })
    resp = _get_client().chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": content}],
        max_tokens=max_tokens,
    )
    return resp.choices[0].message.content or ""
