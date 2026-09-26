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


def _parse_reply(content: str) -> Optional[tuple[str, float, str, str]]:
    """Extract answer/confidence/reason/recognized-stem from model output."""
    text = content.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    else:
        braces = re.search(r"\{.*\}", text, re.S)
        if braces:
            text = braces.group(0)
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, Mapping):
        return None
    answer = data.get("answer")
    confidence = data.get("confidence", data.get("conf", 0))
    if not isinstance(answer, str) or not answer.strip():
        return None
    letters = "".join(dict.fromkeys(re.sub(r"[^A-Za-z]", "", answer).upper()))
    if not letters:
        return None
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        confidence = 0
    reason = data.get("reason", data.get("explanation", data.get("why", "")))
    if not isinstance(reason, str):
        reason = ""
    stem = data.get("text", data.get("question", data.get("stem", "")))
    if not isinstance(stem, str):
        stem = ""
    return letters, max(0.0, min(1.0, float(confidence))), reason.strip(), stem.strip()


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
                error = f"回复无法解析: {content!r:.120}" if isinstance(content, str) else "回复格式异常"
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
