"""AI answer provider for OpenAI-compatible chat/completions endpoints.

Defaults target the Qiniu AI gateway (https://api.qnaigc.com/v1, model
deepseek/deepseek-v4-flash); base_url and model are fully configurable so
any OpenAI-compatible API (SenseNova, DeepSeek official, qwen, glm,
self-hosted, ...) works too.

The API key is read from the environment variable named by ``api_key_env``
(default ``AI_API_KEY``) or from a ``.env`` file in the project root. Never
put keys into source files or JSON configs.
"""

from __future__ import annotations

import base64
import ast
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

DEFAULT_AI_CONFIG: dict[str, Any] = {
    "enabled": False,
    "base_url": "https://4router.net/v1",
    "model": "claude-haiku-4-5-20251001",
    "fallback_models": [],
    "confidence_switch_threshold": 0,
    "ensemble": False,
    "ensemble_models": ["claude-haiku-4-5-20251001", "claude-sonnet-5"],
    "verify_threshold": 0.85,
    "vision": True,
    "api_key_env": "AI_API_KEY",
    "timeout_seconds": 60,
    "max_retries": 2,
    "min_confidence": 0,
    "temperature": 0,
    "request_interval": 6,
    "use_cache": False,
    "cache_path": "answer_books/ai_cache.json",
}

FORBIDDEN_CACHE_PATHS = {"chapter_answers.json"}

SYSTEM_PROMPT = (
    "你是课程答题专家。先仔细阅读题干，逐个分析每个选项为什么对或错，"
    "推理出正确答案后再作答。"
    "只输出一个 JSON 对象，不要输出任何其他文字或代码块标记，格式："
    '{"answer": "字母组合", "confidence": 置信度, "reason": "一句话理由"}。'
    "单选题 answer 恰好一个字母；多选题 answer 为全部正确字母，按字母顺序排列。"
    "confidence 是 0 到 1 的小数，表示你对答案正确的把握；无法确定时给低值。"
    "reason 用一句话说明为什么其他选项不对或该选项正确。"
)

VISION_SYSTEM_PROMPT = (
    "你是课程答题专家。题目以截图形式给出（网页用了自定义字体，DOM 文本不可靠，"
    "必须以图片内容为准）。先仔细读出题干和每个选项，逐个分析为什么对或错，"
    "推理出正确答案后再作答。"
    "只输出一个 JSON 对象，不要输出任何其他文字或代码块标记，格式："
    '{"text": "题干原文", "options": {"A": "选项原文", "B": "..."}, '
    '"answer": "字母组合", "confidence": 置信度, "reason": "一句话理由"}。'
    "单选题 answer 恰好一个字母；多选题 answer 为全部正确字母，按字母顺序排列；"
    "判断题用 A 表示对、B 表示错。"
    "confidence 是 0 到 1 的小数，表示你对答案正确的把握；无法确定时给低值。"
    "reason 用一句话说明为什么其他选项不对或该选项正确。"
    "【格式硬性要求】JSON 必须完整闭合，不要中途截断；"
    "题干或理由里出现的引号一律改用中文引号「」或『』，绝不要写英文双引号，"
    "更不要不转义就使用 \\\" 之外的引号；"
    "text 只填题干本身，不要包含题号、选项或你自己的分析。"
)


def validate_ai_config(value: Any) -> dict[str, Any]:
    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise ValueError("ai 必须是 JSON 对象")
    unknown = set(value) - set(DEFAULT_AI_CONFIG)
    if unknown:
        raise ValueError("ai 配置不支持的项: " + ", ".join(sorted(unknown)))
    result = {**DEFAULT_AI_CONFIG, **value}
    for key in ("base_url", "model", "api_key_env"):
        if not isinstance(result[key], str) or not result[key].strip():
            raise ValueError(f"ai.{key} 必须是非空字符串")
    if not re.fullmatch(r"https?://[^\s]+", result["base_url"].strip()):
        raise ValueError("ai.base_url 必须是 http/https 开头的接口地址，例如 https://token.sensenova.cn/v1")
    result["base_url"] = result["base_url"].strip().rstrip("/")
    if not isinstance(result["enabled"], bool):
        raise ValueError("ai.enabled 必须是 true/false")
    for key in ("timeout_seconds", "max_retries"):
        number = result[key]
        if isinstance(number, bool) or not isinstance(number, int) or number < 0:
            raise ValueError(f"ai.{key} 必须是不小于 0 的整数")
    if result["timeout_seconds"] < 1:
        raise ValueError("ai.timeout_seconds 至少为 1 秒")
    if result["max_retries"] > 5:
        raise ValueError("ai.max_retries 不能超过 5")
    interval = result["request_interval"]
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not 0 <= interval <= 60:
        raise ValueError("ai.request_interval 必须是 0 到 60 之间的数值（相邻两次调用的最小间隔秒数）")
    threshold = result["confidence_switch_threshold"]
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not 0 <= threshold <= 1:
        raise ValueError("ai.confidence_switch_threshold 必须是 0 到 1 之间的数值（低于该置信度时换备用模型重判）")
    verify = result["verify_threshold"]
    if isinstance(verify, bool) or not isinstance(verify, (int, float)) or not 0 <= verify <= 1:
        raise ValueError("ai.verify_threshold 必须是 0 到 1 之间的数值（投票一致但低于该置信度时触发复核）")
    fallbacks = result["fallback_models"]
    if not isinstance(fallbacks, list) or not all(
            isinstance(item, str) and item.strip() for item in fallbacks):
        raise ValueError("ai.fallback_models 必须是模型名字符串列表，例如 [\"deepseek-v4-pro\", \"glm-5.2\"]")
    # Drop duplicates and the primary model: it has already been tried first.
    seen = {result["model"]}
    result["fallback_models"] = [item for item in fallbacks
                                 if not (item in seen or seen.add(item))]
    if not isinstance(result["ensemble"], bool):
        raise ValueError("ai.ensemble 必须是 true/false")
    if not isinstance(result["vision"], bool):
        raise ValueError("ai.vision 必须是 true/false（截图发给视觉模型判题）")
    ensemble = result["ensemble_models"]
    if not isinstance(ensemble, list) or not all(
            isinstance(item, str) and item.strip() for item in ensemble):
        raise ValueError("ai.ensemble_models 必须是模型名字符串列表，例如 [\"deepseek-v4-flash\", \"glm-5.2\", \"deepseek-v4-pro\"]")
    if not 2 <= len(ensemble) <= 5:
        raise ValueError("ai.ensemble_models 需要 2 到 5 个模型：前两个投票，其余依次仲裁")
    result["ensemble_models"] = list(dict.fromkeys(item.strip() for item in ensemble))
    for key in ("min_confidence", "temperature"):
        number = result[key]
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not 0 <= number <= 2:
            raise ValueError(f"ai.{key} 必须是 0 到 2 之间的数值")
    if not 0 <= result["min_confidence"] <= 1:
        raise ValueError("ai.min_confidence 必须在 0 到 1 之间；0 表示不拦截置信度，AI 给什么填什么")
    if not isinstance(result["use_cache"], bool):
        raise ValueError("ai.use_cache 必须是 true/false")
    cache = result["cache_path"]
    if not isinstance(cache, str) or not cache.strip():
        raise ValueError("ai.cache_path 必须是非空字符串")
    pure = cache.strip().replace("\\", "/")
    if Path(pure).name.lower() in FORBIDDEN_CACHE_PATHS:
        raise ValueError("ai.cache_path 不能覆盖脚本自带的 chapter_answers.json")
    if re.search(r'[<>:"|?*\x00-\x1f]', pure):
        raise ValueError("ai.cache_path 含有文件名禁用字符")
    result["cache_path"] = cache.strip()
    return result


def load_api_key(api_key_env: str, base_dir: Path | str) -> Optional[str]:
    """Read the key from the environment, then from a .env file in base_dir."""
    value = os.environ.get(api_key_env, "").strip()
    if value:
        return value
    env_path = Path(base_dir) / ".env"
    if env_path.is_file():
        try:
            lines = env_path.read_text(encoding="utf-8-sig").splitlines()
        except OSError:
            return None
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, entry = line.partition("=")
            if name.strip() == api_key_env:
                entry = entry.strip().strip('"').strip("'")
                if entry:
                    return entry
    return None


def _default_post_json(url: str, headers: Mapping[str, str], payload: Mapping[str, Any],
                       timeout: float) -> str:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


_JSON_FENCE_RE = re.compile(r"```(?:json|JSON|javascript|js)?[ \t]*\r?\n?(.*?)(?:```|\Z)", re.S)
_ANSWER_KEYS = ("answer", "answers", "correct_answer", "correctAnswer", "correct",
                "choice", "choices", "selected", "selection", "答案", "正确答案")
_CONFIDENCE_KEYS = ("confidence", "conf", "certainty", "score", "probability", "置信度", "把握")
_REASON_KEYS = ("reason", "explanation", "why", "rationale", "analysis", "explain",
                "justification", "分析", "理由", "解释")
_STEM_KEYS = ("text", "question", "stem", "title", "content", "题干", "题目")
_OPTIONS_KEYS = ("options", "option", "choices_map", "选项")


def _first_key(data: Mapping[str, Any], names: tuple[str, ...]) -> Any:
    """Case-insensitive lookup of the first matching key."""
    if not isinstance(data, Mapping):
        return None
    lowered = {str(key).strip().lower(): value for key, value in data.items()}
    for name in names:
        if name in data:
            return data[name]
        hit = lowered.get(name.lower())
        if hit is not None:
            return hit
    return None


def _to_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, (list, tuple)):
        parts = [_to_text(item) for item in value]
        return "；".join(part for part in parts if part)
    if isinstance(value, Mapping):
        # {"A": "选项内容"} 之类的结构只取键名拼成字母串由调用方处理
        return ""
    return ""


def _extract_json_objects(text: str) -> list[str]:
    """Collect balanced {...} substrings, tolerant of braces inside strings.

    Handles truncated output: if a candidate never closes, it is still
    returned (with a best-effort closing brace) so the caller can try a
    lenient parse instead of failing outright.
    """
    candidates: list[str] = []
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    candidates.append(text[start:index + 1])
                    start = -1
    if depth > 0 and start >= 0:
        # Truncated tail: try to salvage it by closing strings/braces.
        tail = text[start:]
        if in_string:
            tail += '"'
        tail += "}" * depth
        candidates.append(tail)
    return candidates


def _unescape_stray_quotes(chunk: str) -> str:
    """Escape quotes that appear inside a JSON string value.

    Some models embed Chinese text containing `"` without escaping it, e.g.
    {"text": "走出了"死亡谷"。"} — a strict parser sees the string end early
    and rejects the whole object. We re-scan and escape such quotes, closing
    each string only when the next non-space char looks like JSON structure.
    """
    out: list[str] = []
    in_string = False
    escaped = False
    index = 0
    length = len(chunk)
    while index < length:
        char = chunk[index]
        if escaped:
            out.append(char)
            escaped = False
            index += 1
            continue
        if char == "\\":
            out.append(char)
            escaped = True
            index += 1
            continue
        if char != '"':
            out.append(char)
            index += 1
            continue
        if not in_string:
            in_string = True
            out.append(char)
            index += 1
            continue
        # Inside a string: a quote is a real terminator only if what follows
        # looks like `:` `,` `}` `]` — otherwise it is stray content.
        probe = index + 1
        while probe < length and chunk[probe] in " \t\r\n":
            probe += 1
        following = chunk[probe] if probe < length else ""
        if following in ":,}]" or following == "":
            in_string = False
            out.append(char)
        else:
            out.append('\\"')
        index += 1
    if in_string:
        out.append('"')
    return "".join(out)


def _scan_answers_by_key(chunk: str) -> Optional[dict[str, Any]]:
    """Last-resort: regex out the fields we need from a broken object.

    Used when no amount of quoting repair yields valid JSON (deeply truncated
    replies, unescaped quotes, mixed quote styles).
    """
    if "{" not in chunk:
        return None
    data: dict[str, Any] = {}
    for name in _ANSWER_KEYS:
        match = re.search(rf'"{re.escape(name)}"\s*:\s*"([^"]{{1,40}})"', chunk, re.I)
        if match:
            data["answer"] = match.group(1).strip()
            break
    if not data.get("answer"):
        match = re.search(r'"answer"\s*:\s*([^\s,}"\']{1,20})', chunk, re.I)
        if match:
            data["answer"] = match.group(1).strip()
    if not data.get("answer"):
        return None
    for name in ("confidence", "conf"):
        match = re.search(rf'"{name}"\s*:\s*"?([0-9.]+%?)', chunk)
        if match:
            data["confidence"] = match.group(1)
            break
    for name in ("reason", "explanation", "why"):
        match = re.search(rf'"{name}"\s*:\s*"(.*?)(?<!\\)"', chunk, re.S)
        if match:
            data["reason"] = match.group(1)
            break
    for name in ("text", "question", "stem"):
        match = re.search(rf'"{name}"\s*:\s*"(.*?)(?<!\\)"', chunk, re.S)
        if match:
            data["stem"] = match.group(1)
            break
    return data


def _loads_lenient(chunk: str) -> Optional[Any]:
    """json.loads with small repairs for common model formatting slips."""
    # 0) 先试 ast.literal_eval：对单引号/尾随逗号的容忍度远高于 json。
    try:
        evaluated = ast.literal_eval(chunk)
        if isinstance(evaluated, (dict, list)):
            return evaluated
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        pass

    # 1) 原样 / 归一换行。
    for attempt in (chunk, chunk.replace("\r\n", "\n")):
        try:
            return json.loads(attempt)
        except (json.JSONDecodeError, ValueError):
            pass

    # 2) 转义字符串值里未转义的引号（中文引号最常见，注意要在常规修复之前做，
    #    否则键名引号化会把裸键名也误识别成"字符串内的引号"）。
    variants: list[str] = []
    requoted = _unescape_stray_quotes(chunk)
    if requoted != chunk:
        variants.append(requoted)

    # 3) 常规修复组合。
    repaired = re.sub(r",\s*([}\]])", r"\1", chunk)              # 尾随逗号
    if '"' not in repaired:
        repaired = repaired.replace("'", '"')                    # 纯单引号
    repaired = re.sub(r"([{,]\s*)([A-Za-z_\u4e00-\u9fff][\w\u4e00-\u9fff]*)(\s*:)",
                      r'\1"\2"\3', repaired)                      # 无引号键名
    repaired = re.sub(r":\s*([A-Za-z])\s*([,}])", r': "\1"\2', repaired)  # 裸字母值
    repaired = re.sub(r",\s*([}\]])", r"\1", repaired)
    variants.append(repaired)
    variants.append(_unescape_stray_quotes(repaired))

    for candidate in variants:
        if not candidate:
            continue
        for text in (candidate, re.sub(r",\s*([}\]])", r"\1", candidate)):
            try:
                return json.loads(text)
            except (json.JSONDecodeError, ValueError):
                continue
    return None


def _parse_plain_text(text: str) -> Optional[dict[str, Any]]:
    """Fallback for non-JSON replies: 答案：B / answer is B / **B**."""
    data: dict[str, Any] = {}
    answer_match = re.search(
        r"(?:答案|answer|correct(?:\s+answer)?|选择|choice)\s*[:：为是]?\s*\**\s*([A-Ha-h](?:\s*[,，、/和\s]*[A-Ha-h])*)",
        text, re.I)
    if not answer_match:
        answer_match = re.search(r"\*\*([A-H])\*\*", text)
    if answer_match:
        data["answer"] = answer_match.group(1)
    confidence_match = re.search(r"(?:confidence|置信度|把握)\D{0,6}(\d+(?:\.\d+)?)\s*%?", text, re.I)
    if confidence_match:
        value = float(confidence_match.group(1))
        data["confidence"] = value / 100 if value > 1 else value
    reason_match = re.search(r"(?:reason|理由|解释|分析)\s*[:：]\s*(.+)", text)
    if reason_match:
        data["reason"] = reason_match.group(1).strip()
    return data or None


def _find_answer_object(data: Any, depth: int = 0) -> Optional[Mapping[str, Any]]:
    """Locate the object that actually carries an answer field.

    Handles replies like {"result": {"answer": "A"}, "status": "ok"} — without
    this we would treat the outer object's keys as option letters.
    """
    if depth > 3:
        return None
    if isinstance(data, Mapping):
        if _first_key(data, _ANSWER_KEYS) is not None:
            return data
        for value in data.values():
            found = _find_answer_object(value, depth + 1)
            if found is not None:
                return found
        return None
    if isinstance(data, (list, tuple)):
        for item in data:
            found = _find_answer_object(item, depth + 1)
            if found is not None:
                return found
    return None


def parse_reply_best_effort(content: str) -> Optional[dict[str, Any]]:
    """Best-effort extraction of a model reply, returning a raw dict.

    Handles: ```json fences, leading/trailing prose, multiple JSON objects,
    truncated JSON, trailing commas, unquoted keys, bare-letter values,
    full-width punctuation and non-JSON plain answers.
    """
    if not isinstance(content, str) or not content.strip():
        return None
    text = content.strip()

    # 1) 优先取代码块内容（可能没有闭合的 ```）。
    chunks: list[str] = []
    for match in _JSON_FENCE_RE.finditer(text):
        body = match.group(1).strip()
        if body:
            chunks.append(body)
    chunks.extend(_extract_json_objects(text))
    # 整段直接尝试一次，应对 {"a":1} 之外的数组/对象顶层结构。
    chunks.append(text)

    seen: set[str] = set()
    fallback_scan: Optional[dict[str, Any]] = None
    for chunk in chunks:
        for candidate in _extract_json_objects(chunk) + [chunk]:
            candidate = candidate.strip().strip("`").strip()
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            data = _loads_lenient(candidate)
            found = _find_answer_object(data)
            if found is not None:
                return dict(found)
            # JSON 修不好时，用正则从坏对象里抠出关键字段，留作最后兜底。
            if fallback_scan is None:
                fallback_scan = _scan_answers_by_key(candidate)

    if fallback_scan is not None:
        return fallback_scan

    plain = _parse_plain_text(text)
    if plain is not None:
        return plain

    # 最后兜底：整段里只有孤立的一个大写选项字母。
    stripped = re.sub(r"```.*?```", "", text, flags=re.S).strip()
    lone = re.fullmatch(r"[^A-Za-z]{0,8}([A-Ha-h])[^A-Za-z]{0,8}", stripped)
    if lone:
        return {"answer": lone.group(1), "confidence": 0, "reason": ""}
    return None


def _normalize_confidence(value: Any) -> float:
    """Accept 0.85 / 85 / "85%" / "0.85" and clamp to [0, 1]."""
    if isinstance(value, bool):
        return 0.0
    percent = False
    if isinstance(value, str):
        match = re.search(r"(\d+(?:\.\d+)?)", value)
        if not match:
            return 0.0
        percent = "%" in value
        value = float(match.group(1))
    if not isinstance(value, (int, float)):
        return 0.0
    value = float(value)
    if percent or value > 1.0:
        value = value / 100.0
    return max(0.0, min(1.0, value))


def _parse_reply(content: str) -> Optional[tuple[str, float, str, str]]:
    """Extract answer/confidence/reason/recognized-stem from model output."""
    data = parse_reply_best_effort(content)
    if data is None:
        return None
    answer = _first_key(data, _ANSWER_KEYS)
    letter_source = _to_text(answer)
    if not letter_source and isinstance(answer, Mapping):
        letter_source = "".join(str(key) for key in answer)
    if not letter_source and isinstance(answer, list):
        letter_source = "".join(
            item if isinstance(item, str) else str(_first_key(item, ("letter", "key", "option", "value")) or "")
            for item in answer)
    letters = "".join(dict.fromkeys(re.sub(r"[^A-Za-z]", "", letter_source).upper()))
    if not letters:
        return None
    confidence = _normalize_confidence(_first_key(data, _CONFIDENCE_KEYS))
    reason = _to_text(_first_key(data, _REASON_KEYS))
    stem = _to_text(_first_key(data, _STEM_KEYS))
    if stem and len(stem) > 400:
        stem = stem[:400] + "..."
    if reason and len(reason) > 200:
        reason = reason[:200] + "..."
    return letters, confidence, reason, stem


class AIAnswerProvider:
    """Answers questions by calling an OpenAI-compatible chat/completions API."""

    def __init__(self, config: Mapping[str, Any], base_dir: Path | str, *,
                 verbose: bool = True,
                 post_json: Optional[Callable[[str, Mapping[str, str], Mapping[str, Any], float], str]] = None):
        self.config = validate_ai_config(config)
        self.verbose = verbose
        self.endpoint = self.config["base_url"] + "/chat/completions"
        api_key = load_api_key(self.config["api_key_env"], base_dir)
        if not api_key:
            raise ValueError(
                f"未找到 API key：请设置环境变量 {self.config['api_key_env']}，"
                f"或在 {Path(base_dir) / '.env'} 文件中写一行 {self.config['api_key_env']}=你的key"
            )
        self.api_key = api_key
        self.cache_path = Path(self.config["cache_path"])
        if not self.cache_path.is_absolute():
            self.cache_path = Path(base_dir) / self.cache_path
        self._cache = self._load_cache() if self.config["use_cache"] else {}
        self._post_json = post_json or _default_post_json
        self._next_allowed = 0.0
        # 熔断：同一模型连续失败 N 次后，本次运行内跳过它。
        self._model_fails: dict[str, int] = {}
        self._skip_announced: set[str] = set()

    def _load_cache(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError, ValueError):
            return {}
        if not isinstance(data, Mapping):
            return {}
        return {str(key): value for key, value in data.items()
                if isinstance(value, Mapping) and isinstance(value.get("answer"), str)}

    def _save_cache(self) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.cache_path.with_suffix(self.cache_path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(self._cache, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8", newline="\n")
            os.replace(temporary, self.cache_path)
        except OSError as exc:
            if self.verbose:
                print(f"AI 缓存写入失败（不影响本次作答）: {exc}")

    @staticmethod
    def cache_key(question: Any) -> str:
        payload = {
            "text": getattr(question, "text", ""),
            "kind": getattr(question, "kind", ""),
            "options": [[key, label] for key, label in getattr(question, "options", [])],
        }
        return hashlib.sha1(json.dumps(payload, ensure_ascii=False, sort_keys=True)
                            .encode("utf-8")).hexdigest()

    def _build_prompt(self, question: Any) -> str:
        kind_text = "多选题（正确答案至少一个字母）" if getattr(question, "kind", "") == "multi" else "单选题（answer 恰好一个字母）"
        lines = [f"题型：{kind_text}", "题干：", getattr(question, "text", "").strip(), "选项："]
        lines.extend(f"{key}. {label}" for key, label in getattr(question, "options", []))
        lines.append("请选出正确答案，只输出 JSON。")
        return "\n".join(lines)

    def _build_messages(self, question: Any) -> tuple[str, Any]:
        """Return (system_prompt, user_content); user_content is a multimodal
        list in vision mode (screenshot embedded as base64 data URL)."""
        if not self.config["vision"]:
            return SYSTEM_PROMPT, self._build_prompt(question)
        block = getattr(question, "block", None)
        image_bytes: Optional[bytes] = None
        if block is not None:
            try:
                image_bytes = block.screenshot(type="png")
            except Exception as exc:  # PlaywrightError and friends
                if self.verbose:
                    print(f"题目截图失败（退回 DOM 文本判题）: {exc}")
        if image_bytes:
            encoded = base64.b64encode(image_bytes).decode("ascii")
            return VISION_SYSTEM_PROMPT, [
                {"type": "text",
                 "text": "请阅读图片中的题目并作答。只输出一个 JSON 对象（键名必须是 "
                         "text、options、answer、confidence、reason），不要输出任何其他文字。"},
                {"type": "image_url",
                 "image_url": {"url": "data:image/png;base64," + encoded}},
            ]
        return SYSTEM_PROMPT, self._build_prompt(question)

    def _request(self, question: Any, model: str) -> Optional[tuple[str, float, str, str]]:
        if self._model_fails.get(model, 0) >= 2:
            if model not in self._skip_announced:
                self._skip_announced.add(model)
                if self.verbose:
                    print(f"模型 {model} 近期连续失败，本次运行内跳过它")
            return None
        system_prompt, user_content = self._build_messages(question)
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": self.config["temperature"],
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        attempts = self.config["max_retries"] + 1
        timed_out = False
        for attempt in range(attempts):
            self._throttle()
            try:
                raw = self._post_json(self.endpoint, headers, payload, self.config["timeout_seconds"])
                data = json.loads(raw)
                content = data["choices"][0]["message"]["content"]
                parsed = _parse_reply(content if isinstance(content, str) else "")
                if parsed is not None:
                    self._model_fails[model] = 0
                    return parsed
                if isinstance(content, str):
                    preview = " ".join(content.split())[:160]
                    error = f"回复无法解析（原始内容：{preview!r}）"
                else:
                    error = "回复格式异常（content 不是文本）"
            except urllib.error.HTTPError as exc:
                try:
                    detail = exc.read().decode("utf-8", errors="replace")[:200]
                except OSError:
                    detail = ""
                error = f"HTTP {exc.code}: {detail}"
                if attempt < attempts - 1:
                    if exc.code == 429:
                        wait = 15 + 10 * attempt
                        if self.verbose:
                            print(f"接口限流（429），等待 {wait} 秒后重试...", flush=True)
                        time.sleep(wait)
                        continue
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                error = f"网络错误: {exc}"
                # 超时多为网关/链路瞬时问题，等 60 秒再试两轮代价太大：只补试一次。
                if timed_out and attempt < attempts - 1:
                    break
                if isinstance(getattr(exc, "reason", None), TimeoutError) or isinstance(exc, TimeoutError):
                    timed_out = True
            except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError) as exc:
                error = f"响应格式异常: {exc}"
            if attempt < attempts - 1:
                time.sleep(1 + attempt)
        self._model_fails[model] = self._model_fails.get(model, 0) + 1
        if self.verbose:
            print(f"AI 接口调用失败（已重试）: {error}（模型 {model}）")
        return None

    def _throttle(self) -> None:
        """Keep at least request_interval seconds between actual HTTP calls."""
        interval = self.config["request_interval"]
        if interval <= 0:
            return
        wait = self._next_allowed - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._next_allowed = time.monotonic() + interval

    def _print_question(self, question: Any, number: Any, kind: Any) -> None:
        """Print the stem and options so the terminal log is self-contained."""
        kind_text = "多选题" if kind == "multi" else "单选题"
        vision_note = "，网页字体反爬，以截图识别为准" if self.config.get("vision") else ""
        if self.config.get("vision"):
            # DOM 文本受网页字体反爬影响可能是乱码，不打印题干与选项；
            # 可读题干由视觉模型从截图 OCR 识别，识别结果在下方结果行显示。
            print(f"AI 第{number}题（{kind_text}{vision_note}）：题干由截图 OCR 识别，结果在下方显示")
            return
        text = " ".join(str(getattr(question, "text", "")).split())
        print(f"AI 第{number}题（{kind_text}{vision_note}）题干：{text}")
        for key, label in getattr(question, "options", []):
            label = " ".join(str(label).split())
            print(f"    {key}. {label}")

    def answer(self, question: Any) -> Optional[list[str]]:
        options = list(getattr(question, "options", []))
        if not options:
            return None
        available = {key for key, _ in options}
        number = getattr(question, "number", "?")
        kind = getattr(question, "kind", "single")

        def usable(letters: str) -> bool:
            wanted = set(letters)
            return bool(wanted) and wanted <= available and not (kind == "single" and len(wanted) != 1)

        key = self.cache_key(question)
        if self.config["use_cache"]:
            entry = self._cache.get(key)
            if entry is not None:
                letters = re.sub(r"[^A-Za-z]", "", entry.get("answer", "")).upper()
                if usable(letters):
                    if self.verbose:
                        print(f"AI 第{number}题 → {letters}（缓存命中，不调用接口）")
                    return sorted(letters)

        if self.config["ensemble"]:
            if self.verbose:
                self._print_question(question, number, kind)
            return self._answer_ensemble(question, usable, key, number)

        if self.verbose:
            self._print_question(question, number, kind)
        threshold = self.config["confidence_switch_threshold"]
        model_chain = [self.config["model"], *self.config["fallback_models"]]
        best: Optional[tuple[str, float, str, str, str]] = None  # letters, confidence, model, reason, stem
        for index, model in enumerate(model_chain):
            parsed = self._request(question, model)
            if parsed is None:
                continue
            letters, confidence, reason, stem = parsed
            if not usable(letters):
                if self.verbose:
                    print(f"AI 第{number}题（{model}）→ 答案 {letters} 未通过校验，已放弃该答案")
                continue
            if self.verbose:
                stem_note = f"｜OCR题干：{stem}" if stem else ""
                reason_note = f"｜理由：{reason}" if reason else ""
                print(f"AI 第{number}题（{model}）→ {letters}（置信度 {confidence:.0%}{stem_note}{reason_note}）")
            if best is None or confidence > best[1]:
                best = (letters, confidence, model, reason, stem)
            if confidence >= threshold:
                break
            if self.verbose and index < len(model_chain) - 1:
                print(f"AI 第{number}题 → 置信度 {confidence:.0%} 低于 {threshold:.0%}，换下一个模型重判...")
        if best is None:
            return None
        letters, confidence, model, reason, stem = best
        announced = len(model_chain) == 1 and self.verbose  # 单模型时上面的过程行已打印结果
        if confidence < threshold and len(model_chain) > 1 and self.verbose:
            print(f"AI 第{number}题 → 所有模型均低于 {threshold:.0%}，采用最高置信度结果：{letters}"
                  f"（{model}，置信度 {confidence:.0%}）")
            announced = True
        if confidence < self.config["min_confidence"]:
            if self.verbose:
                print(f"AI 第{number}题 → 置信度 {confidence:.0%} 低于阈值 {self.config['min_confidence']:.0%}，已放弃该答案")
            return None
        if self.config["use_cache"]:
            self._cache[key] = {"answer": letters, "confidence": confidence,
                                "model": model, "reason": reason, "text": stem}
            self._save_cache()
        if self.verbose and not announced:
            model_note = "" if model == self.config["model"] else f"，来自 {model}"
            stem_note = f"｜OCR题干：{stem}" if stem else ""
            reason_note = f"｜理由：{reason}" if reason else ""
            print(f"AI 第{number}题 → {letters}（置信度 {confidence:.0%}{model_note}）{stem_note}{reason_note}")
        return sorted(letters)

    def _answer_ensemble(self, question: Any, usable: Callable[[str], bool],
                         key: str, number: Any) -> Optional[list[str]]:
        """Multi-model voting with low-confidence verification.

        1. First two models vote independently.
        2. Agreement with confidence >= verify_threshold: accept immediately.
        3. Otherwise (disagreement, a voter failed, or low-confidence
           agreement): consult the remaining models one by one. Any answer
           reaching 2 votes (majority) with adequate confidence wins.
        4. A majority that stays low-confidence can still be overruled by a
           dissent holding clearly higher confidence (margin 0.15) — two
           models guessing in agreement are less reliable than one model
           that reasoned its way to high confidence.
        """
        chain = self.config["ensemble_models"]
        verify_threshold = self.config["verify_threshold"]
        candidates: list[tuple[str, float, str, str]] = []  # letters, confidence, model, reason

        def collect(model: str) -> Optional[tuple[str, float, str]]:
            parsed = self._request(question, model)
            if parsed is None:
                return None
            letters, confidence, reason, _stem = parsed
            if not usable(letters):
                if self.verbose:
                    print(f"AI 第{number}题（{model}）→ 答案 {letters} 未通过校验，已放弃该答案")
                return None
            if self.verbose:
                reason_note = f"｜理由：{reason}" if reason else ""
                print(f"AI 第{number}题（{model}）→ 投票 {letters}（置信度 {confidence:.0%}{reason_note}）")
            return letters, confidence, reason

        def side(answer_set: frozenset[str]) -> list[tuple[str, float, str]]:
            return [item for item in candidates if frozenset(item[0]) == answer_set]

        def report(letters: str, confidence: float, models: str) -> None:
            if self.config["use_cache"]:
                self._cache[key] = {"answer": letters, "confidence": confidence, "model": models}
                self._save_cache()

        # First two models vote.
        for model in chain[:2]:
            result = collect(model)
            if result is not None:
                candidates.append((result[0], result[1], model, result[2]))
        if len(candidates) == 2 and frozenset(candidates[0][0]) == frozenset(candidates[1][0]):
            letters, confidence = candidates[0][0], min(candidates[0][1], candidates[1][1])
            if confidence >= verify_threshold:
                report(letters, confidence, "+".join(item[2] for item in candidates))
                if self.verbose:
                    print(f"AI 第{number}题 → {letters}（{candidates[0][2]} 与 {candidates[1][2]} 一致，"
                          f"置信度 {confidence:.0%}）")
                return sorted(letters)
            if self.verbose:
                print(f"AI 第{number}题 → 两模型一致但置信度仅 {confidence:.0%}，"
                      f"追加验证模型复核...")

        # Disagreement / failure / low-confidence agreement: consult the rest.
        for model in chain[2:]:
            result = collect(model)
            if result is None:
                continue
            letters, confidence, reason = result
            mates = side(frozenset(letters))
            if mates:
                # This model joins an existing side: majority of 2 reached.
                side_letters = mates[0][0]
                side_conf = max(confidence, max(item[1] for item in mates))
                side_names = "+".join(item[2] for item in mates) + f"+{model}"
                if side_conf >= verify_threshold:
                    report(side_letters, side_conf, side_names)
                    if self.verbose:
                        print(f"AI 第{number}题 → {side_letters}"
                              f"（{mates[0][2]} 与 {model} 一致，多数票；置信度 {side_conf:.0%}）")
                    return sorted(side_letters)
                # Majority is still low-confidence: keep consulting the
                # remaining models for stronger confirmation or dissent.
                if self.verbose:
                    print(f"AI 第{number}题 → {side_letters}（{mates[0][2]} 与 {model} 一致，多数票，"
                          f"但置信度仅 {side_conf:.0%}，继续复核...）")
                candidates.append((letters, confidence, model, reason))
                continue
            candidates.append((letters, confidence, model, reason))

        if not candidates:
            return None

        # Tally: majority side vs highest-confidence dissent.
        groups: dict[frozenset[str], list[tuple[str, float, str, str]]] = {}
        for item in candidates:
            groups.setdefault(frozenset(item[0]), []).append(item)
        majority = max(groups.values(), key=len)
        majority_conf = max(item[1] for item in majority)
        if len(majority) >= 2:
            dissent = [item for group in groups.values() if group is not majority for item in group]
            top_dissent = max(dissent, key=lambda item: item[1]) if dissent else None
            if top_dissent is not None and top_dissent[1] - majority_conf >= 0.15:
                if self.verbose:
                    print(f"AI 第{number}题 → {top_dissent[0]}（多数票 "
                          f"{'/'.join(item[2] for item in majority)} 低置信度 {majority_conf:.0%}，"
                          f"被 {top_dissent[2]} 的更高置信度 {top_dissent[1]:.0%} 推翻）")
                report(top_dissent[0], top_dissent[1], top_dissent[2])
                return sorted(top_dissent[0])
            letters = majority[0][0]
            if self.verbose:
                note = "" if majority_conf >= verify_threshold else f"，置信度偏低（{majority_conf:.0%}），建议核对"
                print(f"AI 第{number}题 → {letters}（多数票 {'/'.join(item[2] for item in majority)}"
                      f"{note}）")
            report(letters, majority_conf, "+".join(item[2] for item in majority))
            return sorted(letters)

        # No majority at all: fall back to the highest-confidence answer.
        letters, confidence, model, reason = max(candidates, key=lambda item: item[1])
        if confidence < self.config["min_confidence"]:
            if self.verbose:
                print(f"AI 第{number}题 → 模型意见不一致且最高置信度 {confidence:.0%} "
                      f"低于阈值 {self.config['min_confidence']:.0%}，已放弃该答案")
            return None
        if self.verbose:
            disagree = "、".join(f"{item[2]}:{item[0]}" for item in candidates)
            reason_note = f"｜理由：{reason}" if reason else ""
            print(f"AI 第{number}题 → {letters}（模型意见不一致：{disagree}；"
                  f"采用最高置信度 {model}，{confidence:.0%}）{reason_note}")
        report(letters, confidence, model)
        return sorted(letters)
