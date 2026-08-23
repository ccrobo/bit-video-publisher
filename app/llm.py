"""推理模型接入: OpenAI兼容接口调用 / 联通测试 / 发布内容整理"""
import json
import re

import httpx

from .logs import add_log


class LLMError(Exception):
    pass


DEFAULT_PROMPT = """你是一位专业的短视频运营助手。请根据我提供的视频文字材料，为发布平台整理出结构化的发布内容。

必须严格按以下JSON格式输出（不要输出任何其他内容）：
{"title": "标题", "description": "描述文案", "tags": ["标签1", "标签2"]}

要求：
1. title：不超过20字，有吸引力、能引发好奇或共鸣；
2. description：80~200字发布文案，口语化、自然流畅、适合短视频平台，结尾引导互动；
3. tags：3~6个话题标签，不带#号；
4. 只依据材料内容整理，不要编造材料中没有的事实。"""

PLATFORM_HINTS = {
    "douyin": "目标平台是抖音：description 将作为视频简介直接发布，语气活泼，可用适量emoji。",
}


def chat(model_cfg, user_text, system="You are a helpful assistant.", timeout=90):
    """调用 OpenAI 兼容的 chat/completions 接口, 返回模型回复文本"""
    base = (model_cfg.get("api_base") or "").strip().rstrip("/")
    if not base or not (model_cfg.get("model") or "").strip():
        raise LLMError("模型未配置接口地址或模型名称")
    url = f"{base}/chat/completions"
    headers = {"Content-Type": "application/json"}
    key = (model_cfg.get("api_key") or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    payload = {
        "model": model_cfg.get("model"),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_text},
        ],
        "temperature": 0.7,
        "stream": False,
    }
    try:
        # trust_env=False 绕过系统代理, 避免本地/直连请求被代理拦截
        r = httpx.post(url, json=payload, headers=headers, timeout=timeout, trust_env=False)
    except Exception as e:
        raise LLMError(f"请求失败: {e}") from e
    if r.status_code >= 400:
        raise LLMError(f"接口返回 {r.status_code}: {r.text[:200]}")
    try:
        data = r.json()
        return data["choices"][0]["message"]["content"] or ""
    except Exception as e:
        raise LLMError(f"响应解析失败: {e}") from e


def test_model(model_cfg):
    """连通性测试: 发送极短消息验证 Key 与网络可用"""
    reply = chat(model_cfg, "请只回复: pong", system="你是连通性测试助手", timeout=30)
    return (reply or "").strip()[:50]


def parse_content_json(text):
    """解析模型输出的结构化内容JSON(容忍代码块包裹等噪音)"""
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?|```$", "", t.strip(), flags=re.S).strip()
    m = re.search(r"\{.*\}", t, flags=re.S)
    if not m:
        raise LLMError("模型输出中未找到JSON")
    try:
        obj = json.loads(m.group(0))
    except Exception as e:
        raise LLMError(f"模型输出JSON解析失败: {e}") from e
    title = str(obj.get("title") or "").strip()
    desc = str(obj.get("description") or obj.get("desc") or "").strip()
    tags_raw = obj.get("tags")
    if isinstance(tags_raw, str):
        tags = [s.strip().lstrip("#") for s in re.split(r"[,，\s]+", tags_raw) if s.strip()]
    else:
        tags = [str(s).strip().lstrip("#") for s in (tags_raw or []) if str(s).strip()]
    if not desc:
        raise LLMError("模型输出缺少description")
    return {"title": title[:60], "description": desc[:600], "tags": tags[:8]}


def refine_content(model_cfg, platform, material, extra_prompt=""):
    """抓取材料 -> 结构化发布内容 {title, description, tags}

    提示词优先级: 任务覆盖提示词 > 全局默认提示词(DEFAULT_PROMPT)
    """
    system = "你是短视频内容运营专家，只输出符合要求的JSON。"
    parts = [(extra_prompt or "").strip() or DEFAULT_PROMPT]
    hint = PLATFORM_HINTS.get(platform)
    if hint:
        parts.append(f"补充要求：{hint}")
    mat = (material or "").strip()
    if not mat:
        raise LLMError("没有可用的文字材料")
    parts.append("——以下是视频的文字材料——\n" + mat[:4000])
    text = chat(model_cfg, "\n\n".join(parts), system=system)
    add_log(f"AI整理: 模型[{model_cfg.get('name')}] 已返回结果")
    return parse_content_json(text)
