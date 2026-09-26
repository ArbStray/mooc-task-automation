"""Question-bank (题库) lookup provider.

Queries an external question-bank API (default: ZERO 网课题库,
https://api.gomooc.net/api.php) with the question stem and maps the
returned answer text back onto the page's option letters. The API is
keyless and free; it must not be hammered, so requests are throttled.

Chain position: 答案册 → 题库 → AI. A bank hit is preferred over AI
because bank answers are human-verified; anything the bank cannot match
falls through to the AI provider unchanged.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

DEFAULT_TIKU_CONFIG: dict[str, Any] = {
    "enabled": False,
    "api_url": "https://api.gomooc.net/api.php",
    "token": "",
    "timeout_seconds": 15,
    "max_retries": 1,
    "request_interval": 2,
}

# Separators commonly used by question banks between multiple answers.
ANSWER_SEPARATORS = re.compile(r"\s*(?:---|##|#|;;|;|；|，|,|、|\||/|\s)\s*")
# Leading question number like "12." / "3、" / "（4）".
QUESTION_NUMBER_PREFIX = re.compile(r"^\s*[(（]?\d{1,3}[)）]?\s*[.、．,，:：]?\s*")
# Leading option marker like "A." / "B、" / "C " at the start of a fragment.
OPTION_MARKER = re.compile(r"^\s*[A-Za-z]\s*[.、．)）:：]?\s*")


def validate_tiku_config(value: Any) -> dict[str, Any]:
    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise ValueError("tiku 必须是 JSON 对象")
    unknown = set(value) - set(DEFAULT_TIKU_CONFIG)
    if unknown:
        raise ValueError("tiku 配置不支持的项: " + ", ".join(sorted(unknown)))
    result = {**DEFAULT_TIKU_CONFIG, **value}
    for key in ("api_url", "token"):
        if not isinstance(result[key], str):
            raise ValueError(f"tiku.{key} 必须是字符串")
    result["token"] = result["token"].strip()
    url = result["api_url"].strip()
    if not re.fullmatch(r"https?://[^\s]+", url):
        raise ValueError("tiku.api_url 必须是 http/https 开头的接口地址，例如 https://api.gomooc.net/api.php")
    result["api_url"] = url
    if not isinstance(result["enabled"], bool):
        raise ValueError("tiku.enabled 必须是 true/false")
    for key in ("timeout_seconds", "max_retries"):
        number = result[key]
        if isinstance(number, bool) or not isinstance(number, int) or number < 0:
            raise ValueError(f"tiku.{key} 必须是不小于 0 的整数")
    if result["timeout_seconds"] < 1:
        raise ValueError("tiku.timeout_seconds 至少为 1 秒")
    if result["max_retries"] > 5:
        raise ValueError("tiku.max_retries 不能超过 5")
    interval = result["request_interval"]
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not 0 <= interval <= 60:
        raise ValueError("tiku.request_interval 必须是 0 到 60 之间的数值（相邻两次调用的最小间隔秒数）")
    return result


def _normalize(text: str) -> str:
    """Lowercase and drop whitespace/punctuation so bank answers can be
    compared with page options written slightly differently."""
    return re.sub(r"[\s，。、．·,.;；:：!！?？'\"“”‘’()（）\[\]【】<>《》\-—_~*#@&+=|/\\]+", "", text).lower()


def _strip_option_marker(text: str) -> str:
    return OPTION_MARKER.sub("", text.strip())


def extract_stem(question: Any) -> str:
    """Cut the question stem out of the extracted block text.

    The extractor hands us the whole block (stem + options). Cut at the
    earliest option label occurrence and drop the leading question number.
    """
    text = (getattr(question, "text", "") or "").strip()
    cut = len(text)
    for _, label in getattr(question, "options", []):
        marker = label.strip()
        if not marker:
            continue
        index = text.find(marker)
        if index != -1 and index < cut:
            cut = index
    stem = text[:cut].strip()
    stem = QUESTION_NUMBER_PREFIX.sub("", stem).strip()
    return stem


class TikuProvider:
    """Answers questions by looking them up in an external question bank."""

    def __init__(self, config: Mapping[str, Any], *, verbose: bool = True,
                 get_json: Optional[Callable[[str, float], str]] = None):
        self.config = validate_tiku_config(config)
        self.verbose = verbose
        self._get_json = get_json or _default_get_json
        self._next_allowed = 0.0

    def _throttle(self) -> None:
        interval = self.config["request_interval"]
        if interval <= 0:
            return
        wait = self._next_allowed - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._next_allowed = time.monotonic() + interval

    def _query(self, stem: str) -> list[dict[str, Any]]:
        params = {"question": stem}
        if self.config["token"]:
            params["token"] = self.config["token"]
        separator = "&" if "?" in self.config["api_url"] else "?"
        url = self.config["api_url"] + separator + urllib.parse.urlencode(params)
        attempts = self.config["max_retries"] + 1
        for attempt in range(attempts):
            self._throttle()
            try:
                raw = self._get_json(url, self.config["timeout_seconds"])
                data = json.loads(raw)
                if not isinstance(data, Mapping) or data.get("code") not in (200, "200"):
                    if self.verbose:
                        print(f"题库接口返回异常: {str(data)[:120]!r}")
                    return []
                items = data.get("data")
                if not isinstance(items, list):
                    return []
                return [item for item in items if isinstance(item, Mapping) and isinstance(item.get("answer"), str)]
            except urllib.error.HTTPError as exc:
                error = f"HTTP {exc.code}"
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                error = f"网络错误: {exc}"
            except (json.JSONDecodeError, ValueError) as exc:
                error = f"响应格式异常: {exc}"
            if attempt < attempts - 1:
                time.sleep(1)
        if self.verbose:
            print(f"题库接口调用失败（已重试）: {error}")
        return []

    def _match_entry(self, answer_text: str, question: Any) -> Optional[list[str]]:
        """Map a bank answer string onto the page's option letters."""
        options = list(getattr(question, "options", []))
        if not options or not answer_text.strip():
            return None
        kind = getattr(question, "kind", "single")
        # Letters only, e.g. "A" / "BC": trust them directly.
        compact = re.sub(r"[\s,，、;；/|]+", "", answer_text).upper()
        if compact and re.fullmatch(r"[A-Z]+", compact):
            return list(dict.fromkeys(compact))
        letters: list[str] = []
        for fragment in ANSWER_SEPARATORS.split(answer_text):
            fragment = _strip_option_marker(fragment)
            if not fragment:
                continue
            normalized = _normalize(fragment)
            if not normalized:
                continue
            for key, label in options:
                if key in letters:
                    continue
                label_text = _normalize(_strip_option_marker(label))
                if not label_text:
                    continue
                # Require a real overlap: containment with at least 2 chars,
                # or the fragment is (almost) exactly the label.
                if (normalized == label_text
                        or (len(normalized) >= 2 and normalized in label_text)
                        or (len(label_text) >= 2 and label_text in normalized)):
                    letters.append(key)
                    break
        if not letters:
            return None
        if kind == "single" and len(letters) != 1:
            return None  # Ambiguous single-choice: let the AI provider decide.
        return list(dict.fromkeys(letters))

    def answer(self, question: Any) -> Optional[list[str]]:
        if not getattr(question, "options", []):
            return None
        stem = extract_stem(question)
        if not stem:
            return None
        number = getattr(question, "number", "?")
        available = {key for key, _ in question.options}
        for entry in self._query(stem):
            matched = self._match_entry(entry.get("answer", ""), question)
            if matched and set(matched) <= available and not (
                    getattr(question, "kind", "single") == "single" and len(matched) != 1):
                if self.verbose:
                    print(f"题库 第{number}题 → {''.join(matched)}"
                          f"（答案原文: {str(entry.get('answer', ''))[:30]!r}）")
                return sorted(matched)
        return None


def _default_get_json(url: str, timeout: float) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "course-task-script/2.4"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")
