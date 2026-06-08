"""共享的轻量网络工具：JSON 磁盘缓存 + 带退避/Retry-After 的请求重试。

Semantic Scholar 抗限流和开源情况联网核验都复用这里的能力，避免在两处各写一份
缓存与退避逻辑。所有请求都通过模块级的 ``requests``，便于测试 patch。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import requests

logger = logging.getLogger(__name__)

DEFAULT_RETRY_STATUSES = (429, 500, 502, 503, 504)


class JsonDiskCache:
    """按 key 哈希存储的 JSON 磁盘缓存，带版本号与 TTL。

    ``ttl_days < 0`` 表示永不过期；命中返回 ``data`` 字段，未命中或过期返回 ``None``。
    """

    def __init__(self, directory: str | Path, ttl_days: int = 3, version: int = 1) -> None:
        self.directory = Path(directory)
        self.ttl_days = int(ttl_days)
        self.version = int(version)

    def _path_for(self, key: str) -> Path:
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:24]
        return self.directory / f"{digest}.json"

    def get(self, key: str) -> Any | None:
        path = self._path_for(key)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if int(payload.get("version") or 0) != self.version:
            return None
        if not self._is_fresh(str(payload.get("fetched_at") or "")):
            return None
        return payload.get("data")

    def set(self, key: str, data: Any) -> None:
        path = self._path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": self.version,
            "key": key,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "data": data,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _is_fresh(self, fetched_at_raw: str) -> bool:
        if self.ttl_days < 0:
            return True
        fetched_at = _parse_iso(fetched_at_raw)
        if fetched_at is None:
            return False
        return datetime.now(timezone.utc) - fetched_at <= timedelta(days=self.ttl_days)


def request_with_retry(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    json: Any = None,
    timeout: int = 30,
    max_retries: int = 3,
    backoff_base: float = 2.0,
    max_backoff: float = 30.0,
    respect_retry_after: bool = True,
    retry_statuses: tuple[int, ...] = DEFAULT_RETRY_STATUSES,
    sleep: Callable[[float], None] = time.sleep,
) -> requests.Response:
    """发起 HTTP 请求，对 429/5xx 和网络异常按指数退避重试。

    退避会优先采用响应里的 ``Retry-After``（若 ``respect_retry_after``）。重试次数用尽后
    返回最后一次的响应（可能仍是 429，由调用方判断），网络异常用尽后向上抛出。
    """

    attempts = max(int(max_retries), 1)
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = requests.request(method, url, headers=headers, params=params, json=json, timeout=timeout)
        except requests.RequestException as exc:
            last_exc = exc
            if attempt >= attempts:
                raise
            sleep(_backoff_delay(attempt, backoff_base, max_backoff))
            continue
        if response.status_code in retry_statuses and attempt < attempts:
            delay = _retry_after_seconds(response, max_backoff) if respect_retry_after else None
            if delay is None:
                delay = _backoff_delay(attempt, backoff_base, max_backoff)
            logger.warning(
                "请求 %s 返回 %s，%.1fs 后重试 (%d/%d)",
                url,
                response.status_code,
                delay,
                attempt,
                attempts,
            )
            sleep(delay)
            continue
        return response
    raise RuntimeError(f"request_with_retry 重试用尽: {url}: {last_exc}")


def _backoff_delay(attempt: int, backoff_base: float, max_backoff: float) -> float:
    return min(float(max_backoff), float(backoff_base) ** attempt)


def _retry_after_seconds(response: requests.Response, max_backoff: float) -> float | None:
    raw = response.headers.get("Retry-After") if getattr(response, "headers", None) else None
    if not raw:
        return None
    try:
        return min(float(max_backoff), max(0.0, float(str(raw).strip())))
    except (TypeError, ValueError):
        return None


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
