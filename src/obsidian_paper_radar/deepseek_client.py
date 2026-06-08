from __future__ import annotations

import json
import logging
import time
from time import perf_counter
from typing import Any

import requests

logger = logging.getLogger(__name__)


class DeepSeekClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepseek.com",
        model_fast: str = "deepseek-v4-flash",
        model_pro: str = "deepseek-v4-pro",
        timeout_fast: int = 60,
        timeout_pro: int = 300,
        max_tokens_fast: int = 8192,
        max_tokens_pro: int = 16384,
        max_retries: int = 3,
        stream: bool = True,
        disable_thinking: bool = False,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model_fast = model_fast
        self.model_pro = model_pro
        # 分模型超时：pro 是推理模型，单次调用常需 2-3 分钟，必须给更长读取窗口。
        self.timeout_fast = timeout_fast
        self.timeout_pro = timeout_pro
        self.max_tokens_fast = max_tokens_fast
        self.max_tokens_pro = max_tokens_pro
        self.max_retries = max_retries
        # SSE 流式读取：模型思考阶段也会持续吐数据，避免因长时间无数据触发读取超时。
        self.stream = stream
        # 关闭 DeepSeek v4 思考模式：更快，但推理深度下降（尤其 pro 精读），默认关闭。
        self.disable_thinking = disable_thinking

    def chat_json(self, messages: list[dict[str, str]], model: str = "fast") -> Any:
        model_name = self.model_fast if model == "fast" else self.model_pro
        timeout = self.timeout_pro if model == "pro" else self.timeout_fast
        max_tokens = self.max_tokens_pro if model == "pro" else self.max_tokens_fast
        payload = {
            "model": model_name,
            "messages": messages,
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
            "max_tokens": max_tokens,
        }
        if self.disable_thinking:
            # DeepSeek v4 关闭思考模式（api-docs.deepseek.com/guides/thinking_mode）。
            payload["thinking"] = {"type": "disabled"}
        if self.stream:
            payload["stream"] = True
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            started = perf_counter()
            try:
                logger.info(
                    "调用 DeepSeek JSON：model=%s attempt=%d/%d timeout=%ss stream=%s",
                    model_name, attempt, self.max_retries, timeout, self.stream,
                )
                if self.stream:
                    content = self._post_stream(payload, headers, timeout)
                else:
                    content = self._post_once(payload, headers, timeout)
                logger.info(
                    "DeepSeek JSON 返回：model=%s attempt=%d elapsed=%.1fs",
                    model_name, attempt, perf_counter() - started,
                )
                logger.debug("DeepSeek raw response: %s", content)
                return _parse_json_payload(content)
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "DeepSeek JSON call failed (%d/%d, model=%s, elapsed=%.1fs): %s",
                    attempt, self.max_retries, model_name, perf_counter() - started, exc,
                )
                if attempt < self.max_retries:
                    # pro 模型需要分钟级恢复，退避上限放大到 30s：3s → 9s → 27s。
                    time.sleep(min(3**attempt, 30))
        raise RuntimeError(f"DeepSeek 调用失败: {last_error}") from last_error

    def _post_once(self, payload: dict[str, Any], headers: dict[str, str], timeout: int) -> str:
        response = requests.post(
            f"{self.base_url}/chat/completions",
            headers=headers,
            json=payload,
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"]

    def _post_stream(self, payload: dict[str, Any], headers: dict[str, str], timeout: int) -> str:
        """SSE 流式读取：逐块拼接 content，思考期间持续有数据，规避读取超时。"""
        parts: list[str] = []
        with requests.post(
            f"{self.base_url}/chat/completions",
            headers=headers,
            json=payload,
            timeout=timeout,
            stream=True,
        ) as response:
            response.raise_for_status()
            for line in response.iter_lines(decode_unicode=True):
                if not line:
                    continue
                if not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if not data or data == "[DONE]":
                    if data == "[DONE]":
                        break
                    continue
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                piece = delta.get("content")
                if piece:
                    parts.append(piece)
        content = "".join(parts)
        if not content.strip():
            raise ValueError("DeepSeek 流式响应为空")
        return content


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    return text


def _find_json_start(text: str, idx: int) -> int | None:
    for i in range(idx, len(text)):
        if text[i] in "{[":
            return i
    return None


def _iter_top_level_json(text: str) -> list[Any]:
    """逐个解析出文本里的顶层 JSON 对象/数组。

    弱模型（如 flash）有时不套外壳、把每条记录当独立对象一个接一个吐出，
    或在合法对象后跟重复/解释文字。朴素的「第一个 { 到最后一个 }」切片会把多个
    顶层对象连在一起，`json.loads` 报 `Extra data`。这里用 `raw_decode` 顺序读取，
    跳过对象之间的噪声，把每个完整对象单独取出来。
    """
    decoder = json.JSONDecoder()
    values: list[Any] = []
    idx = 0
    while idx < len(text):
        start = _find_json_start(text, idx)
        if start is None:
            break
        try:
            value, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            idx = start + 1
            continue
        values.append(value)
        idx = end
    return values


def _parse_json_payload(content: str) -> Any:
    """把 DeepSeek 返回内容稳健地解析成 JSON。

    - 单个顶层值：直接返回；
    - 多个顶层对象：优先返回已带 `items` 外壳的那个（其余多半是重复/尾随噪声），
      否则把这些裸对象聚合成 `{"items": [...]}`，挽救「没套外壳」的输出。
    """
    text = _strip_code_fence(content)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    values = _iter_top_level_json(text)
    if not values:
        raise ValueError(f"无法从 DeepSeek 响应中解析出 JSON：{content[:200]!r}")
    if len(values) == 1:
        return values[0]
    dict_values = [v for v in values if isinstance(v, dict)]
    for value in dict_values:
        if "items" in value:
            return value
    if dict_values:
        return {"items": dict_values}
    return values[0]
