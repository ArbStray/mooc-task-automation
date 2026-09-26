#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Course task runner with explicit answers and verified state transitions."""

from __future__ import annotations

import argparse
import hashlib
import math
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import urlparse

try:
    import msvcrt
except ImportError:
    msvcrt = None

from answer_books import (
    CHAPTER_PATTERN, DEFAULT_BOOK_LABEL, DEFAULT_BOOK_PATH, chapter_groups,
    choose_book, normalize_chapter, option_letters, read_json, split_answers,
)
from ai_provider import DEFAULT_AI_CONFIG, AIAnswerProvider, validate_ai_config
from tiku_provider import DEFAULT_TIKU_CONFIG, TikuProvider, validate_tiku_config

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import Locator, Page, sync_playwright
except ModuleNotFoundError:
    # Configuration/answer checks remain usable without a browser installation.
    Locator = Page = Any
    PlaywrightError = RuntimeError
    sync_playwright = None


ROOT = Path(__file__).resolve().parent
# Bare decimals elsewhere on the page must not become two-level chapter IDs.
FULLTEXT_CHAPTER_PATTERN = r"(?<![\d.])([0-9]+(?:\.[0-9]+){2,})(?![\d.])"
NEXT_PATTERN = r"^\s*(?:下一节|下一步|下一章|Next(?: chapter)?)\s*[>›»→]?\s*$"
PREVIOUS_PATTERN = r"^\s*[<‹«←]?\s*(?:上一节|上一步|上一章|Previous)\s*$"
SUBMIT_PATTERN = r"^\s*(?:提交|提交答案|提交作业|交卷|Submit)\s*$"
DIALOG_SELECTORS = (
    "[role=dialog]", "[role=alertdialog]", ".el-dialog", ".layui-layer", ".modal", ".dialog",
    ".ant-modal", ".ant-modal-confirm", ".arco-modal", ".weui-dialog", ".van-dialog",
    ".t-dialog", ".n-modal", ".semi-modal", ".cube-dialog", "[class*='dialog'][class*='wrap']",
)

DEFAULT_CONFIG: dict[str, Any] = {
    "next_selectors": ["[data-action='next']", ".nextChapter", ".next-btn"],
    "submit_selectors": ["button[type=submit]", "input[type=submit]", "[data-action='submit']", ".submit-btn"],
    "video_selectors": ["video", ".video-js video"],
    "question_selectors": [
        "[data-question-id]", ".TiMu", ".questionLi", ".question", ".quiz-question",
        "[id^='question_']", "li:has(input[type=radio])", "li:has(input[type=checkbox])",
    ],
    "reading_selectors": ["article", ".reading-content", ".material-content", "[data-task-type='reading']"],
    "empty_task_selectors": ["[data-task-type='empty']"],
    "loading_selectors": ["[aria-busy='true']", ".loading", ".spinner", ".el-loading-mask"],
    "chapter_selectors": [
        "[data-current-chapter]", "[aria-current='page']", "[aria-current='step']",
        ".posCatalog_active", ".chapter.active", ".chapter.current", "h1", "h2",
    ],
    "selected_option_selectors": [
        "[aria-checked='true']", "[aria-selected='true']", "[data-selected='true']",
        ".selected", ".checked", ".active", ".on",
    ],
    "exercise_success_selectors": ["[data-submit-status='success']", ".submit-success", ".submit-successfully",
                                   ".el-message--success", ".el-message-box__wrapper", ".toast-success",
                                   "[class*='success']", "[class*='submitted']"],
    # 提交成功后页面文案往往还带得分/题数等（如"提交成功，共 5 题"），
    # 用子串匹配而非整行锚定；失败标记先行判断，避免误报。
    "exercise_success_pattern": r"提交成功|交卷成功|作答成功|提交完成|提交已完成|完成学习|"
                                r"本次(?:测验|作业|练习|单元)(?:已提交|已完成|提交完成)|Submission successful",
    "exercise_error_pattern": r"提交失败|提交异常|交卷失败|网络异常|Submission failed",
    "video_threshold": 1.0,
    "video_stall_timeout_seconds": 120,
    "completion_wait_seconds": 3,
    "next_click_interval": 4.5,
    "poll_seconds": 1,
    "reading_scroll_pause": 0.2,
    "reading_min_seconds": 2,
    "task_timeout_seconds": 1800,
    "manual_exercise_timeout_seconds": 3600,
    "page_ready_timeout": 15,
    "next_button_timeout": 20,
    "submit_timeout_seconds": 25,
    # 提交后是否等待可验证的“提交成功”提示再进入下一节。
    # false：点完提交并处理确认弹窗后直接进入下一节（不做成功验证）。
    # true：沿用旧的等待验证逻辑，确认到成功标记才继续。
    "verify_submit_success": False,
    "empty_grace_seconds": 3,
    "chapter_answers": {},
    "ai": DEFAULT_AI_CONFIG,
    "tiku": DEFAULT_TIKU_CONFIG,
}


def validate_config(config: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(config, Mapping):
        raise ValueError("配置文件顶层必须是 JSON 对象")
    unknown = set(config) - set(DEFAULT_CONFIG)
    if unknown:
        raise ValueError("不支持的配置项: " + ", ".join(sorted(unknown)))
    result = {**DEFAULT_CONFIG, **config}
    result["ai"] = validate_ai_config(result.get("ai"))
    result["tiku"] = validate_tiku_config(result.get("tiku"))
    for key, value in result.items():
        if key.endswith("_selectors"):
            if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
                raise ValueError(f"{key} 必须是选择器字符串列表")
        elif key.endswith("_seconds") or key in {"page_ready_timeout", "next_button_timeout", "next_click_interval", "reading_scroll_pause"}:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{key} 必须是有限数值")
            allow_zero = key in {"completion_wait_seconds", "next_click_interval", "reading_scroll_pause", "reading_min_seconds"}
            if value < 0 or (value == 0 and not allow_zero):
                raise ValueError(f"{key} 超出有效范围")
        elif key == "verify_submit_success":
            if not isinstance(value, bool):
                raise ValueError("verify_submit_success 必须是 true/false")
        elif key.endswith("_pattern"):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{key} 必须是非空正则表达式")
            try:
                re.compile(value)
            except re.error as exc:
                raise ValueError(f"{key} 正则表达式无效: {exc}") from exc
    threshold = result["video_threshold"]
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not 0 < threshold <= 1:
        raise ValueError("video_threshold 必须大于 0 且不超过 1")
    if not isinstance(result["chapter_answers"], Mapping):
        raise ValueError("chapter_answers 必须是章节号到答案的映射")
    normalized_chapters = {}
    for chapter, answers in result["chapter_answers"].items():
        chapter = normalize_chapter(chapter)
        if chapter in normalized_chapters:
            raise ValueError(f"章节号重复: {chapter}")
        chapter_groups(answers)
        normalized_chapters[chapter] = answers
    result["chapter_answers"] = normalized_chapters
    return result


@dataclass
class Question:
    number: int
    question_id: str
    text: str
    kind: str
    options: list[tuple[str, str]]
    block: Locator


class StaticAnswerProvider:
    def __init__(self, answers: Mapping[str, Any]):
        self.answers = {str(key): value for key, value in answers.items()}

    def answer(self, question: Question) -> Optional[list[str]]:
        digest = hashlib.sha1(question.text.encode("utf-8")).hexdigest()
        for key in (question.question_id, digest, str(question.number)):
            if key and key in self.answers:
                return option_letters(self.answers[key])
        return None


def apply_answer_book(config: Mapping[str, Any], answers: Any) -> tuple[dict[str, Any], StaticAnswerProvider]:
    """A selected book is the only answer source, never an overlay on an old book."""
    chapters, explicit = split_answers(answers)
    return validate_config({**config, "chapter_answers": chapters}), StaticAnswerProvider(explicit)


class TaskRunner:
    def __init__(self, page: Page, config: Mapping[str, Any], provider: Any, auto_submit: bool,
                 auto_answer: bool = False, ai_provider: Any = None, tiku_provider: Any = None):
        self.page = page
        self.config = validate_config(config)
        self.provider = provider
        self.auto_submit = auto_submit
        self.auto_answer = auto_answer
        self.ai_provider = ai_provider
        self.tiku_provider = tiku_provider
        self._skip_next_click = False
        self._allow_submit_dialog = False
        self._dialog_error = ""
        if hasattr(page, "on"):
            page.on("dialog", self._handle_dialog)

    def _handle_dialog(self, dialog: Any) -> None:
        message = dialog.message
        if re.search(self.config["exercise_error_pattern"], message, re.I):
            self._dialog_error = message
            dialog.dismiss()
            return
        # alert（提交成功提示等）一律确认；confirm 仅在提交流程中确认。
        if dialog.type == "alert" or (self._allow_submit_dialog and dialog.type == "confirm"):
            dialog.accept()
            return
        dialog.dismiss()

    def _visible(self, scope: Any, selector: str) -> Iterable[Locator]:
        try:
            matches = scope.locator(selector)
            for index in range(matches.count()):
                item = matches.nth(index)
                if item.is_visible():
                    yield item
        except PlaywrightError:
            return

    def _first_visible(self, selectors: Iterable[str]) -> Optional[Locator]:
        return next((item for selector in selectors for item in self._visible(self.page, selector)), None)

    def _first_visible_any_frame(self, selectors: Iterable[str]) -> Optional[Locator]:
        for frame in self.page.frames:
            for selector in selectors:
                found = next(iter(self._visible(frame, selector)), None)
                if found is not None:
                    return found
        return None

    def _text_control(self, pattern: str, scopes: Optional[Iterable[Any]] = None) -> Optional[Locator]:
        rx = re.compile(pattern, re.I)
        for scope in self.page.frames if scopes is None else scopes:
            try:
                for role in ("button", "link"):
                    matches = scope.get_by_role(role, name=rx)
                    for index in range(matches.count()):
                        item = matches.nth(index)
                        if item.is_visible() and item.is_enabled():
                            return item
                # Exact anchored patterns also support clickable spans/divs.
                matches = scope.get_by_text(rx)
                for index in range(matches.count()):
                    item = matches.nth(index)
                    if item.is_visible() and item.is_enabled():
                        return item
            except PlaywrightError:
                continue
        return None

    @staticmethod
    def _click_control(control: Locator) -> bool:
        try:
            if not control.is_visible() or not control.is_enabled():
                return False
            control.click(timeout=3000)
            return True
        except PlaywrightError as exc:
            print(f"控件点击失败: {exc}")
            return False

    def _all_frame_text(self) -> str:
        parts = []
        for frame in self.page.frames:
            try:
                parts.append(frame.locator("body").inner_text(timeout=1000))
            except PlaywrightError:
                continue
        return "\n".join(parts)

    def current_chapter(self) -> Optional[str]:
        for selector in self.config["chapter_selectors"]:
            candidates = set()
            for frame in self.page.frames:
                for item in self._visible(frame, selector):
                    try:
                        text = (item.get_attribute("data-current-chapter") or "") + " " + item.inner_text(timeout=500)
                        candidates.update(normalize_chapter(value) for value in re.findall(CHAPTER_PATTERN, text))
                    except PlaywrightError:
                        continue
            if len(candidates) == 1:
                return candidates.pop()
        text = self._all_frame_text()
        explicit = re.findall(r"(?:当前章节|正在学习|Current chapter)\s*[:：]?\s*" + CHAPTER_PATTERN, text, re.I)
        explicit = {normalize_chapter(value) for value in explicit}
        if len(explicit) == 1:
            return explicit.pop()
        if explicit:
            return None
        candidates = {normalize_chapter(value) for value in re.findall(FULLTEXT_CHAPTER_PATTERN, text)}
        return candidates.pop() if len(candidates) == 1 else None

    def chapter_answer(self, question: Question) -> Optional[list[str]]:
        chapter = self.current_chapter()
        raw = self.config["chapter_answers"].get(chapter)
        if raw is None:
            return None
        groups = chapter_groups(raw)
        return groups[question.number - 1] if 1 <= question.number <= len(groups) else None

    def _task_from_elements(self) -> str:
        for kind, key in (
            ("video", "video_selectors"), ("exercise", "question_selectors"),
            ("reading", "reading_selectors"), ("empty", "empty_task_selectors"),
        ):
            if self._first_visible_any_frame(self.config[key]) is not None:
                return kind
        return "unknown"

    def _document_ready(self) -> bool:
        if self._first_visible_any_frame(self.config["loading_selectors"]) is not None:
            return False
        try:
            return all(frame.evaluate("() => document.readyState === 'complete'") for frame in self.page.frames)
        except PlaywrightError:
            return False

    def detect_task(self) -> str:
        kind = self._task_from_elements()
        if kind != "unknown":
            return kind
        # Preserve blank-chapter navigation, but allow delayed tasks to appear first.
        if (self._next_control() is None and self._text_control(PREVIOUS_PATTERN) is None) or not self._document_ready():
            return "unknown"
        before = self.page_signature()
        try:
            self.page.wait_for_load_state("networkidle", timeout=self.config["page_ready_timeout"] * 1000)
        except PlaywrightError:
            return "unknown"
        self.page.wait_for_timeout(self.config["empty_grace_seconds"] * 1000)
        kind = self._task_from_elements()
        if kind != "unknown":
            return kind
        if self._document_ready() and before and self.page_signature() == before:
            return "empty"
        return "unknown"

    def page_signature(self) -> str:
        parts = [self.page.url, self.current_chapter() or ""]
        has_content = False
        for frame in self.page.frames:
            try:
                text = frame.locator("body").inner_text(timeout=1000)
                sources = frame.locator("video").evaluate_all(
                    "nodes => nodes.map(el => el.currentSrc || el.src || '').join('|')"
                )
                has_content = has_content or bool(text.strip() or sources)
                text = re.sub(r"\b\d{1,2}:\d{2}(?::\d{2})?\b|\d+(?:\.\d+)?%", "<progress>", text)
                parts.extend((frame.url, re.sub(r"\s+", " ", text), sources))
            except PlaywrightError:
                continue
        return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest() if has_content else ""

    def wait_for_task_ready(self, previous_signature: Optional[str] = None, timeout: Optional[float] = None) -> bool:
        deadline = time.monotonic() + (timeout if timeout is not None else self.config["page_ready_timeout"])
        last = ""
        stable = 0
        while time.monotonic() < deadline:
            signature = self.page_signature()
            if signature and signature != previous_signature and self._document_ready():
                stable = stable + 1 if signature == last else 0
                last = signature
                if stable >= 2:
                    return True
            else:
                stable, last = 0, ""
            self.page.wait_for_timeout(500)
        return False

    def run_video(self) -> bool:
        videos = []
        # The first selector normally covers all videos; avoid duplicate players.
        for frame in self.page.frames:
            for selector in self.config["video_selectors"]:
                found = list(self._visible(frame, selector))
                if found:
                    videos.extend(found)
                    break
        if not videos:
            return False
        for video in videos:
            deadline = time.monotonic() + self.config["task_timeout_seconds"]
            last_progress = time.monotonic()
            last_current = -1.0
            confirmations = 0
            try:
                while time.monotonic() < deadline:
                    state = video.evaluate("el => ({current: el.currentTime, duration: el.duration, ended: el.ended, paused: el.paused, error: !!el.error})")
                    current = float(state.get("current") or 0)
                    duration = float(state.get("duration") or 0)
                    if state.get("error"):
                        print("视频播放器报告错误，已暂停。")
                        return False
                    complete = state.get("ended") or (
                        self.config["video_threshold"] < 1 and math.isfinite(duration) and duration > 0
                        and current / duration >= self.config["video_threshold"]
                    )
                    if complete:
                        confirmations += 1
                        if confirmations >= 3:
                            break
                    else:
                        confirmations = 0
                        if state.get("paused"):
                            played = video.evaluate("el => { el.muted = true; return el.play().then(() => true).catch(() => false); }")
                            if not played:
                                print("浏览器未能播放视频，请手动检查播放器。")
                                return False
                        if current > last_current:
                            last_current, last_progress = current, time.monotonic()
                        if time.monotonic() - last_progress > self.config["video_stall_timeout_seconds"]:
                            print("视频长时间未推进，已暂停。")
                            return False
                    self.page.wait_for_timeout(self.config["poll_seconds"] * 1000)
                else:
                    print("视频等待超时，已暂停。")
                    return False
            except PlaywrightError as exc:
                print(f"视频操作失败: {exc}")
                return False
        self.page.wait_for_timeout(self.config["completion_wait_seconds"] * 1000)
        return True

    def run_reading(self) -> bool:
        script = """selectors => {
            const visible = el => !!(el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden');
            const roots = selectors.flatMap(selector => Array.from(document.querySelectorAll(selector))).filter(visible);
            if (!roots.length) return {found: false, bottom: false};
            const targets = new Set();
            for (const root of roots) {
                const nodes = [root, ...root.querySelectorAll('*')];
                for (const node of nodes) {
                    if (visible(node) && node.clientHeight > 0 && node.scrollHeight > node.clientHeight + 2
                        && /(auto|scroll|overlay)/.test(getComputedStyle(node).overflowY)) targets.add(node);
                }
                let ancestor = root.parentElement;
                while (ancestor) {
                    if (ancestor === document.scrollingElement
                        || /(auto|scroll|overlay)/.test(getComputedStyle(ancestor).overflowY)) {
                        targets.add(ancestor);
                        break;
                    }
                    ancestor = ancestor.parentElement;
                }
                if (!ancestor && document.scrollingElement) targets.add(document.scrollingElement);
            }
            for (const node of targets) node.scrollTop = Math.min(node.scrollHeight, node.scrollTop + Math.max(300, node.clientHeight * 0.8));
            return {found: true, bottom: [...targets].every(node => node.scrollHeight - node.clientHeight - node.scrollTop <= 3)};
        }"""
        started = time.monotonic()
        deadline = started + self.config["task_timeout_seconds"]
        stable_bottom = 0
        while time.monotonic() < deadline:
            states = []
            for frame in self.page.frames:
                try:
                    states.append(frame.evaluate(script, self.config["reading_selectors"]))
                except PlaywrightError as exc:
                    print(f"读取阅读区域失败: {exc}")
                    return False
            found = [state for state in states if state.get("found")]
            if not found:
                print("未定位到可验证的阅读区域，请检查 reading_selectors。")
                return False
            stable_bottom = stable_bottom + 1 if all(state.get("bottom") for state in found) else 0
            if stable_bottom >= 3 and time.monotonic() - started >= self.config["reading_min_seconds"]:
                self.page.wait_for_timeout(self.config["completion_wait_seconds"] * 1000)
                return True
            self.page.wait_for_timeout(max(100, self.config["reading_scroll_pause"] * 1000))
        print("阅读区域未能在时限内滚动到底部，已暂停。")
        return False

    def _question_blocks(self) -> list[Locator]:
        result = []
        identity_script = """el => {
            const path = [];
            for (let node = el; node && node.parentElement; node = node.parentElement)
                path.unshift(Array.prototype.indexOf.call(node.parentElement.children, node));
            return '/' + path.join('/') + '/';
        }"""
        for frame in self.page.frames:
            paths = []
            frame_blocks = []
            for selector in self.config["question_selectors"]:
                for block in self._visible(frame, selector):
                    try:
                        path = block.evaluate(identity_script)
                    except PlaywrightError:
                        continue
                    if any(path.startswith(old) or old.startswith(path) for old in paths):
                        continue
                    paths.append(path)
                    frame_blocks.append((tuple(int(part) for part in path.split("/") if part), block))
            result.extend(block for _, block in sorted(frame_blocks, key=lambda item: item[0]))
        return result

    @staticmethod
    def _text_option_key(text: str, index: int) -> str:
        match = re.match(r"\s*([A-Za-z])(?:[\s.、)）:：]|$)", text)
        return match.group(1).upper() if match else chr(65 + index)

    @staticmethod
    def _option_key(inp: Locator, index: int) -> str:
        value = (inp.get_attribute("value") or "").strip()
        return value.upper() if re.fullmatch(r"[A-Za-z]", value) else chr(65 + index)

    @staticmethod
    def _custom_option_locators(block: Locator) -> list[Locator]:
        for selector in ("[role=radio], [role=checkbox]", "label", "[class*=option]", "[class*=choice]"):
            locators = block.locator(selector)
            items = [locators.nth(index) for index in range(locators.count()) if locators.nth(index).is_visible()]
            if len(items) >= 2:
                return items
        return []

    def _extract_questions(self) -> list[Question]:
        questions = []
        for index, block in enumerate(self._question_blocks(), start=1):
            text = block.inner_text(timeout=2000).strip()
            number_text = block.get_attribute("data-question-number") or ""
            numbered = re.match(r"^\s*(\d+)\s*[.、)）]", text)
            number = int(number_text) if number_text.isdigit() else int(numbered.group(1)) if numbered else index
            qid = block.get_attribute("data-question-id") or block.get_attribute("id") or ""
            inputs = block.locator("input[type=radio], input[type=checkbox]")
            options = []
            is_multi = False
            for option_index in range(inputs.count()):
                inp = inputs.nth(option_index)
                key = self._option_key(inp, option_index)
                label = inp.locator("xpath=ancestor::label[1]")
                label_text = label.inner_text().strip() if label.count() else key
                options.append((key, label_text))
                is_multi = is_multi or inp.get_attribute("type") == "checkbox"
            if not options:
                for option_index, control in enumerate(self._custom_option_locators(block)):
                    label_text = control.inner_text(timeout=500).strip()
                    options.append((self._text_option_key(label_text, option_index), label_text))
                    is_multi = is_multi or control.get_attribute("role") == "checkbox"
                is_multi = is_multi or "多选" in text
            # Unsupported questions must remain visible to the completeness check.
            questions.append(Question(number, str(qid), text, "multi" if is_multi else "single", options, block))
        return questions

    def _custom_selected(self, control: Locator) -> bool:
        return bool(control.evaluate(
            "(el, selectors) => selectors.some(selector => el.matches(selector))",
            self.config["selected_option_selectors"],
        ))

    def _select_question_answer(self, question: Question, wanted: set[str]) -> bool:
        available = {key for key, _ in question.options}
        if not wanted or not wanted <= available or (question.kind == "single" and len(wanted) != 1):
            return False
        try:
            inputs = question.block.locator("input[type=radio], input[type=checkbox]")
            if inputs.count():
                for index in range(inputs.count()):
                    inp = inputs.nth(index)
                    should_check = self._option_key(inp, index) in wanted
                    if inp.get_attribute("type") == "radio" and not should_check:
                        continue
                    if inp.is_checked() != should_check:
                        if inp.is_visible():
                            inp.set_checked(should_check, timeout=3000)
                        else:
                            label = inp.locator("xpath=ancestor::label[1]")
                            if not label.count() or not self._click_control(label.first):
                                return False
                selected = {self._option_key(inputs.nth(index), index) for index in range(inputs.count()) if inputs.nth(index).is_checked()}
                return selected == wanted
            controls = self._custom_option_locators(question.block)
            for index, control in enumerate(controls):
                key = self._text_option_key(control.inner_text(timeout=500).strip(), index)
                selected = self._custom_selected(control)
                should_check = key in wanted
                if question.kind == "single" and not should_check:
                    continue
                if selected != should_check and not self._click_control(control):
                    return False
            selected = {
                self._text_option_key(control.inner_text(timeout=500).strip(), index)
                for index, control in enumerate(controls) if self._custom_selected(control)
            }
            return selected == wanted
        except PlaywrightError as exc:
            print(f"答案选择失败: {exc}")
            return False

    def _exercise_status(self) -> str:
        text = self._all_frame_text()
        if self._dialog_error or re.search(self.config["exercise_error_pattern"], text, re.I):
            return "error"
        if self._first_visible_any_frame(self.config["exercise_success_selectors"]) is not None:
            return "success"
        return "success" if re.search(self.config["exercise_success_pattern"], text, re.I) else "pending"

    def _page_location(self) -> str:
        try:
            return self.page.url + "|" + (self.current_chapter() or "")
        except PlaywrightError:
            return self.page.url

    def _enter_pressed(self) -> bool:
        if msvcrt is None:
            return False
        try:
            pressed = False
            while msvcrt.kbhit():
                if msvcrt.getwch() in ("\r", "\n"):
                    pressed = True
            return pressed
        except OSError:
            return False

    def wait_manual_exercise(self, preface: str = "本节练习请在浏览器中自行作答并提交。") -> bool:
        """练习由用户手动作答：等待完成后再继续下一节。"""
        if self._exercise_status() == "success":
            return True
        print(preface)
        if msvcrt is None:
            print("当前系统不支持终端按键通知，脚本只会自动检测提交成功提示。")
        else:
            print("完成后回到此终端按 Enter 通知脚本继续；脚本也会自动检测提交成功提示。")
        location = self._page_location()
        deadline = time.monotonic() + self.config["manual_exercise_timeout_seconds"]
        allow_force = False
        while time.monotonic() < deadline:
            if self._exercise_status() == "success":
                print("已检测到练习提交成功，继续下一节。")
                break
            if self._enter_pressed():
                if self._exercise_status() == "success":
                    print("已确认练习提交成功，继续下一节。")
                    break
                if not allow_force:
                    allow_force = True
                    print("未检测到提交成功提示；如确已完成，请再按一次 Enter 强制继续，否则继续等待。")
                else:
                    print("已按你的确认继续；请自行核对上一节的完成状态。")
                    break
            self.page.wait_for_timeout(self.config["poll_seconds"] * 1000)
        else:
            print("等待练习完成超时，已暂停。")
            return False
        if self._page_location() != location:
            print("检测到页面已切换到其他章节，本次不自动点击下一节。")
            self._skip_next_click = True
        self.page.wait_for_timeout(self.config["completion_wait_seconds"] * 1000)
        return True

    def run_exercise(self) -> bool:
        if not self.auto_answer:
            return self.wait_manual_exercise()
        self._dialog_error = ""
        if self._exercise_status() == "success":
            return True
        questions = self._extract_questions()
        if not questions:
            print("未提取到题目，已暂停。")
            return False
        # Resolve the chapter once so a DOM change cannot mix chapters mid-quiz.
        chapter = self.current_chapter()
        raw_chapter = self.config["chapter_answers"].get(chapter)
        groups = chapter_groups(raw_chapter) if raw_chapter is not None else []
        resolved = [(question, self.provider.answer(question) if self.provider is not None else None)
                    for question in questions]
        # When the book exists but question numbers cannot line up with it,
        # AI may answer each question on its own instead of pausing.
        book_mismatch = False
        if groups and any(not answer for _, answer in resolved):
            numbers = sorted(question.number for question in questions)
            if numbers != list(range(1, len(groups) + 1)):
                if self.ai_provider is None and self.tiku_provider is None:
                    print(f"章节 {chapter} 的答案共 {len(groups)} 题，页面识别到 {len(questions)} 题，"
                          "题号未完整对应；请检查答案册、题目分页或编号变化。已暂停，未填写或提交。")
                    return False
                book_mismatch = True
                print(f"章节 {chapter} 的答案共 {len(groups)} 题，页面识别到 {len(questions)} 题，"
                      "题号未完整对应；答案册按题号对位不可用，改由题库/AI 逐题作答。")
        for index, (question, answer) in enumerate(resolved):
            if not answer and not book_mismatch and 1 <= question.number <= len(groups):
                answer = groups[question.number - 1]
            if not answer and self.tiku_provider is not None:
                answer = self.tiku_provider.answer(question)
            if not answer and self.ai_provider is not None:
                answer = self.ai_provider.answer(question)
            resolved[index] = (question, answer)
        planned = []
        missing = []
        for question, answer in resolved:
            if not answer and not book_mismatch and 1 <= question.number <= len(groups):
                answer = groups[question.number - 1]
            wanted = set(answer or [])
            available = {key for key, _ in question.options}
            if (
                not wanted or not wanted <= available or len(available) != len(question.options)
                or (question.kind == "single" and len(wanted) != 1)
            ):
                missing.append(question.question_id or str(question.number))
            else:
                planned.append((question, wanted))
        if missing:
            print(f"当前答案册与题目未匹配，已暂停，不会改用其他答案册。"
                  f"章节: {chapter or '未识别'}；题号/ID: {', '.join(missing)}")
            return False
        for question, wanted in planned:
            if not self._select_question_answer(question, wanted):
                print(f"无法确认第 {question.number} 题的选中状态，已暂停。")
                return False
        if not self.auto_submit:
            # Answers are filled but not submitted: keep the browser open and
            # wait for the user to review and submit, then continue.
            return self.wait_manual_exercise(
                "答案已填入，请在浏览器中核对并提交；脚本会自动检测提交成功后继续"
                "（也可在提交后回终端按 Enter）。")
        submit = self._text_control(SUBMIT_PATTERN) or self._first_visible_any_frame(self.config["submit_selectors"])
        if submit is None:
            print("未找到提交按钮，已暂停。")
            return False
        self._allow_submit_dialog = True
        try:
            if not self._click_control(submit):
                return False
            if not self.config["verify_submit_success"]:
                # 默认：不验证提交成功。点击提交后短暂处理确认弹窗，然后直接进入下一节。
                # 确认弹窗可能延迟弹出，这里给它约 3 秒（或 submit_timeout_seconds 取小）的时间，反复点掉。
                click_deadline = min(self.config["submit_timeout_seconds"], 3.0)
                dialog_deadline = time.monotonic() + click_deadline
                while time.monotonic() < dialog_deadline:
                    if self._exercise_status() == "error":
                        print("页面报告提交失败，已暂停。")
                        return False
                    self._confirm_exercise_submit()
                    self.page.wait_for_timeout(400)
                return True
            # 验证模式（verify_submit_success: true）：等待成功提示后再进入下一节。
            deadline = time.monotonic() + self.config["submit_timeout_seconds"]
            while time.monotonic() < deadline:
                status = self._exercise_status()
                if status == "error":
                    print("页面报告提交失败，已暂停。")
                    return False
                if status == "success":
                    return True
                self._confirm_exercise_submit()
                self.page.wait_for_timeout(500)
        finally:
            self._allow_submit_dialog = False
        # 验证模式下超时：给出诊断信息。
        print("未收到可验证的提交成功提示，已暂停；请核对结果或调整成功标记配置，"
              "或在配置中设置 verify_submit_success=false 不再验证、提交完直接进入下一节。")
        # 诊断：列出页面文本中与提交/成功/失败相关的片段，便于判断是否真已提交。
        try:
            text = self._all_frame_text()
            hits = [line.strip() for line in text.splitlines()
                    if re.search(r"提交|交卷|成功|失败|得分|成绩|submit", line, re.I)]
            for hit in hits[:12]:
                print(f"  页面文本: {hit[:120]}")
        except PlaywrightError:
            pass
        return False

    CONFIRM_BUTTON_PATTERN = (
        r"^\s*(?:确认提交|确定提交|确认交卷|提交作业|再检查一下|立即提交|"
        r"提交|确认|确定|是的?|好的?|交卷|Submit|Confirm|OK|Yes)\s*$"
    )
    CONFIRM_PREFIX_PATTERN = r"^\s*(?:确认|确定|提交|交卷)"

    def _visible_dialogs(self) -> list[Locator]:
        dialogs: list[Locator] = []
        for frame in self.page.frames:
            for selector in DIALOG_SELECTORS:
                for dialog in self._visible(frame, selector):
                    dialogs.append(dialog)
        return dialogs

    def _confirm_exercise_submit(self) -> bool:
        # 已在提交流程中：出现的弹窗基本都是提交确认，按多层放宽匹配。
        # 1) 可见弹窗内按键文字完全匹配确认词（最稳）。
        for dialog in self._visible_dialogs():
            button = self._text_control(self.CONFIRM_BUTTON_PATTERN, (dialog,))
            if button is not None and self._click_control(button):
                return True
        # 2) 提交确认弹窗兜底：弹窗文案含"提交/交卷"，但按钮文案未完全匹配时，
        #    找前缀为提交/确认/确定/交卷的按钮点击（覆盖"确认提交作业"类文案）。
        for dialog in self._visible_dialogs():
            try:
                dialog_text = dialog.inner_text(timeout=500)
            except PlaywrightError:
                continue
            if not re.search(r"提交|交卷|submit", dialog_text, re.I):
                continue
            button = self._text_control(self.CONFIRM_PREFIX_PATTERN, (dialog,))
            if button is not None and self._click_control(button):
                return True
        # 3) 兜底：全局查找"确认提交"类按钮（弹窗结构不在选择器列表时）。
        for pattern in (
            r"^\s*(?:确认提交|确定提交|确认交卷|提交作业|立即提交)\s*$",
            r"^\s*(?:确认|确定|提交|交卷)\s*$",
        ):
            fallback = self._text_control(pattern)
            if fallback is not None and self._click_control(fallback):
                return True
        return False

    def _dismiss_incomplete_dialog(self) -> bool:
        text = self._all_frame_text()
        if not any(marker in text for marker in ("任务点未完成", "是否去完成", "当前章节还有")):
            return False
        close = self._text_control(r"^\s*(?:关闭|取消|Close|Cancel|×)\s*$")
        if close is not None:
            self._click_control(close)
        print("平台提示当前任务未完成，自动流程已暂停。")
        return True

    def _next_control(self) -> Optional[Locator]:
        return self._text_control(NEXT_PATTERN) or self._first_visible_any_frame(self.config["next_selectors"])

    def is_terminal_page(self) -> bool:
        return self._next_control() is None and self._text_control(PREVIOUS_PATTERN) is not None

    def _close_success_dialog(self) -> None:
        """提交成功提示弹窗可能遮挡“下一节”按钮，点下一节前先尝试关闭。"""
        for frame in self.page.frames:
            for selector in DIALOG_SELECTORS:
                for dialog in self._visible(frame, selector):
                    try:
                        text = dialog.inner_text(timeout=500)
                    except PlaywrightError:
                        continue
                    if not re.search(r"成功|已完成|已提交|完成学习", text, re.I):
                        continue
                    for pattern in (r"^\s*(?:知道了|确定|确认|关闭|OK|Close)\s*$",):
                        button = self._text_control(pattern, (dialog,))
                        if button is not None and self._click_control(button):
                            return

    def click_next(self) -> bool:
        self._close_success_dialog()
        previous = self.page_signature()
        deadline = time.monotonic() + self.config["next_button_timeout"]
        button = self._next_control()
        while button is None and time.monotonic() < deadline:
            self.page.wait_for_timeout(500)
            button = self._next_control()
        if button is None or not self._click_control(button):
            print("未能点击下一节，已暂停。")
            return False
        self.page.wait_for_timeout(500)
        if self._dismiss_incomplete_dialog():
            return False
        if not self.wait_for_task_ready(previous):
            print("点击后未确认章节变化或页面稳定，已暂停。")
            return False
        if self._dismiss_incomplete_dialog():
            return False
        return True

    def run(self, max_tasks: int) -> bool:
        if not self.wait_for_task_ready():
            print("任务页面加载超时，已暂停。")
            return False
        handlers = {"video": self.run_video, "reading": self.run_reading, "exercise": self.run_exercise, "empty": lambda: True}
        for index in range(1, max_tasks + 1):
            kind = self.detect_task()
            print(f"[{index}/{max_tasks}] 任务类型: {kind}；章节: {self.current_chapter() or '未确定'}")
            if kind not in handlers or not handlers[kind]():
                print("自动流程暂停，请检查当前页面及配置。")
                return False
            if kind == "exercise":
                self.page.wait_for_timeout(self.config["completion_wait_seconds"] * 1000)
            if index == max_tasks:
                print("已达到本次任务数量上限。")
                return True
            # 提交成功弹窗可能仍遮挡"下一节"按钮，或页面还停留在结果视图；
            # 先关闭成功弹窗并等待按钮出现，再判断是否真的没有下一节。
            self._close_success_dialog()
            self.page.wait_for_timeout(500)
            if self.is_terminal_page():
                print("当前页面没有下一节，流程结束。")
                return True
            if self._skip_next_click:
                self._skip_next_click = False
            elif not self.click_next():
                return False
            self.page.wait_for_timeout(self.config["next_click_interval"] * 1000)
        return True


def load_json(path: Optional[str], default: Any) -> Any:
    return read_json(path) if path else default


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="视频/阅读自动处理，练习由用户手动作答后继续")
    parser.add_argument("--url", required=True, help="课程任务页的 http/https URL")
    parser.add_argument("--config", default=str(ROOT / "config.example.json"), help="JSON 配置文件")
    answer_source = parser.add_mutually_exclusive_group()
    answer_source.add_argument("--answers", default=None, help="指定整本答案册 JSON；仅 --auto-answer 模式使用")
    answer_source.add_argument("--choose-answers", action="store_true", help="启动前列出答案册；仅 --auto-answer 模式使用")
    parser.add_argument("--auto-answer", action="store_true", help="练习页按答案册自动填答（旧行为；默认为手动答题）")
    parser.add_argument("--ai-answer", action="store_true",
                        help="用 AI 接口判定答案并自动填入；单独使用为纯 AI 模式，与 --auto-answer 同用为答案册之后的兜底")
    parser.add_argument("--auto-submit", action="store_true", help="核对答案后允许自动提交练习；--auto-answer 或 --ai-answer 模式使用")
    parser.add_argument("--max-tasks", type=int, default=100)
    browser_mode = parser.add_mutually_exclusive_group()
    browser_mode.add_argument("--headed", dest="headed", action="store_true", help="显示浏览器，默认开启")
    browser_mode.add_argument("--headless", dest="headed", action="store_false", help="使用已有会话，无交互运行")
    parser.set_defaults(headed=True)
    parser.add_argument("--user-data-dir", default=str(ROOT / ".course-browser"), help="本地登录会话目录")
    args = parser.parse_args(argv)
    parsed = urlparse(args.url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        parser.error("--url 必须是有效的 http/https 地址")
    if args.max_tasks < 1:
        parser.error("--max-tasks 必须大于 0")
    if args.ai_answer and not args.auto_answer and (args.choose_answers or args.answers):
        parser.error("纯 AI 模式不选择答案册；如需答案册+AI 兜底请同时加 --auto-answer")
    if not args.auto_answer and not args.ai_answer and (args.choose_answers or args.answers):
        parser.error("手动答题模式下不使用答案册；如需自动填答请加 --auto-answer")
    auto_answer = args.auto_answer or args.ai_answer
    if args.choose_answers and (not args.headed or not sys.stdin.isatty()):
        parser.error("--choose-answers 需要交互终端；无人值守运行请用 --answers 指定答案册")
    try:
        ai_provider = None
        tiku_provider = None
        if auto_answer:
            if args.ai_answer and not args.auto_answer:
                config = validate_config({**load_json(args.config, {}), "chapter_answers": {}})
                provider = StaticAnswerProvider({})
                print("纯 AI 模式：不加载答案册，练习题全部由 AI 接口判定后填入。")
            else:
                answer_path = choose_book() if args.choose_answers else Path(args.answers or DEFAULT_BOOK_PATH)
                config = validate_config(load_json(args.config, {}))
                answers = load_json(str(answer_path), {})
                if config["chapter_answers"]:
                    print("提示：已忽略配置中的 chapter_answers，本次只使用所选答案册。")
                config, provider = apply_answer_book(config, answers)
                book_name = DEFAULT_BOOK_LABEL if answer_path.resolve() == DEFAULT_BOOK_PATH.resolve() else answer_path.stem
                print(f"本次答案册：{book_name}（{len(config['chapter_answers'])} 个章节，{len(provider.answers)} 条题目映射）")
                print(f"答案文件：{answer_path.resolve()}")
                print("请确认所选答案册对应当前课程；程序无法判断人工答案是否正确。")
            if args.ai_answer or config["ai"]["enabled"]:
                try:
                    ai_provider = AIAnswerProvider(config["ai"], ROOT)
                    print(f"AI 接口：{config['ai']['model']} @ {config['ai']['base_url']}")
                    if config["ai"]["vision"]:
                        print("AI 模式：题目截图发给视觉模型判题（绕过网页字体反爬）。")
                    print(f"AI 缓存：{ai_provider.cache_path.resolve()}")
                    if args.auto_submit:
                        print("注意：已开启自动提交，AI 答案填入后将直接提交，不做人工核对。")
                    else:
                        print("AI 答案由模型判定，不保证正确；本次只填入不提交，请核对后手动提交。")
                except ValueError as exc:
                    if args.ai_answer:
                        raise
                    print(f"提示：AI 接口未启用（{exc}），继续按答案册作答。")
            if config["tiku"]["enabled"]:
                tiku_provider = TikuProvider(config["tiku"])
                print(f"题库接口：{config['tiku']['api_url']}")
                print("题库答案优先于 AI 判定；命中结果终端会打印答案原文，便于核对。")
        else:
            config = validate_config({**load_json(args.config, {}), "chapter_answers": {}})
            provider = StaticAnswerProvider({})
            if args.auto_submit:
                print("提示：--auto-submit 仅在 --auto-answer 模式下生效，已忽略。")
            print("手动答题模式：视频、阅读和空白章节自动处理；练习由你在浏览器中作答，完成后按 Enter 继续。")
    except (OSError, ValueError) as exc:
        print(f"配置或答案文件错误: {exc}", file=sys.stderr)
        return 2
    except (KeyboardInterrupt, EOFError):
        print("\n已取消，未启动浏览器。")
        return 130
    if sync_playwright is None:
        print("未安装 Playwright，请先运行 install.cmd。", file=sys.stderr)
        return 2
    if args.headed and not sys.stdin.isatty():
        print("可视模式需要交互终端；无人值守运行请使用 --headless 和已有登录会话。", file=sys.stderr)
        return 2
    try:
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(Path(args.user_data_dir).expanduser().resolve()), headless=not args.headed,
                device_scale_factor=2,
            )
            try:
                context.set_default_timeout(5000)
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(args.url, wait_until="domcontentloaded", timeout=60000)
                if args.headed:
                    input("请完成登录并进入具体任务页，按 Enter 开始... ")
                    # Login/course links may have opened a new tab.
                    page = next((tab for tab in reversed(context.pages) if not tab.is_closed() and tab.url != "about:blank"), page)
                # A malformed selector must fail visibly instead of producing an empty task.
                for key, selectors in config.items():
                    if key.endswith("_selectors"):
                        for selector in selectors:
                            page.locator(selector).count()
                result = TaskRunner(page, config, provider, args.auto_submit, auto_answer,
                                    ai_provider, tiku_provider).run(args.max_tasks)
                if args.headed:
                    input("流程已结束或暂停，可在浏览器中核对结果；按 Enter 关闭浏览器... ")
                return 0 if result else 2
            finally:
                context.close()
    except (PlaywrightError, OSError) as exc:
        print(f"运行失败: {exc}", file=sys.stderr)
        return 2
    except (KeyboardInterrupt, EOFError):
        print("\n已停止。")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
