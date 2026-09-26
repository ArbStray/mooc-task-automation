"""Shared, browser-independent answer parsing and whole-course answer books."""

from __future__ import annotations

import json
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional


ROOT = Path(__file__).resolve().parent
BOOKS_DIR = ROOT / "answer_books"
DEFAULT_BOOK_PATH = ROOT / "chapter_answers.json"
DEFAULT_BOOK_LABEL = "默认答案册（脚本自带）"
CHAPTER_KEY_PATTERN = r"[0-9]+(?:\.[0-9]+)+"
CHAPTER_PATTERN = rf"(?<![\d.])({CHAPTER_KEY_PATTERN})(?![\d.])"


def option_letters(value: Any) -> Optional[list[str]]:
    if isinstance(value, list):
        if not value or not all(isinstance(item, str) for item in value):
            return None
        value = "".join(value)
    if not isinstance(value, str):
        return None
    compact = re.sub(r"[\s,，()]+", "", unicodedata.normalize("NFKC", value).upper())
    return list(dict.fromkeys(compact)) if re.fullmatch(r"[A-Z]+", compact) else None


def chapter_groups(value: Any) -> list[list[str]]:
    if isinstance(value, list):
        groups = [option_letters(item) for item in value]
        if not groups or any(group is None for group in groups):
            raise ValueError("章节答案列表中包含无效选项")
        return groups
    if not isinstance(value, str):
        raise ValueError("章节答案必须是字符串或列表")
    compact = re.sub(r"[\s,，]+", "", unicodedata.normalize("NFKC", value).upper())
    tokens = re.findall(r"\([A-Z]+\)|[A-Z]", compact)
    if not tokens or "".join(tokens) != compact:
        raise ValueError("答案格式不正确；单选写 A，多选写 (BC)，例如 A(BC)D 表示三道题")
    return [list(dict.fromkeys(token.strip("()"))) for token in tokens]


def normalize_chapter(chapter: str) -> str:
    if not isinstance(chapter, str):
        raise ValueError("章节号必须是文字，例如 1.1.2")
    chapter = unicodedata.normalize("NFKC", chapter).strip()
    if not re.fullmatch(CHAPTER_KEY_PATTERN, chapter):
        raise ValueError(f"无效章节号：{chapter!r}；请填写页面上的章节号，例如 1.2 或 1.1.2")
    return ".".join(str(int(part)) for part in chapter.split("."))


def compact_answers(value: Any) -> str:
    return "".join(group[0] if len(group) == 1 else "(" + "".join(group) + ")"
                   for group in chapter_groups(value))


def validate_chapter_book(data: Any) -> dict[str, str]:
    if not isinstance(data, Mapping) or not data:
        raise ValueError("答案册至少需要一个章节的答案")
    result = {}
    for key, value in data.items():
        chapter = normalize_chapter(key)
        if chapter in result:
            raise ValueError(f"章节号重复：{chapter}；每个章节只能填写一次")
        try:
            result[chapter] = compact_answers(value)
        except ValueError as exc:
            raise ValueError(f"章节 {chapter}：{exc}") from exc
    return dict(sorted(result.items(), key=lambda item: tuple(map(int, item[0].split(".")))))


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON 中有重复的键：{key}")
        result[key] = value
    return result


def read_json(path: Path | str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"), object_pairs_hook=_unique_pairs)


def load_chapter_book(path: Path | str) -> dict[str, str]:
    return validate_chapter_book(read_json(path))


def split_answers(data: Any) -> tuple[dict[str, str], dict[str, Any]]:
    """Keep legacy explicit question IDs/hashes usable with --answers."""
    if not isinstance(data, Mapping):
        raise ValueError("答案文件顶层必须是 JSON 对象")
    chapters, questions = {}, {}
    for key, value in data.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError("答案键必须是非空文字")
        if re.fullmatch(CHAPTER_KEY_PATTERN, unicodedata.normalize("NFKC", key).strip()):
            chapter = normalize_chapter(key)
            if chapter in chapters:
                raise ValueError(f"章节号重复：{chapter}")
            chapters[chapter] = compact_answers(value)
        else:
            if option_letters(value) is None:
                raise ValueError(f"题目答案格式无效：{key}")
            questions[key] = value
    return chapters, questions


def parse_book_text(text: str) -> dict[str, str]:
    chapters = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = unicodedata.normalize("NFKC", raw).strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(rf"({CHAPTER_KEY_PATTERN})(?:\s*[:=]\s*|\s+)(.+)", line)
        if not match:
            raise ValueError(f"第 {number} 行格式不正确；请按“1.1.2 = A(BC)D”填写，一行一个章节")
        chapter = normalize_chapter(match[1])
        if chapter in chapters:
            raise ValueError(f"第 {number} 行章节 {chapter} 重复；不会覆盖前一行的答案")
        try:
            chapters[chapter] = compact_answers(match[2])
        except ValueError as exc:
            raise ValueError(f"第 {number} 行，章节 {chapter}：{exc}") from exc
    return validate_chapter_book(chapters)


def format_book_text(chapters: Mapping[str, Any]) -> str:
    return "\n".join(f"{chapter} = {answers}" for chapter, answers in validate_chapter_book(chapters).items())


@dataclass(frozen=True)
class BookInfo:
    name: str
    path: Path
    is_default: bool = False


def list_books(directory: Path = BOOKS_DIR) -> list[BookInfo]:
    directory = Path(directory)
    books = [BookInfo(DEFAULT_BOOK_LABEL, DEFAULT_BOOK_PATH, True)]
    if directory.is_dir():
        books.extend(
            BookInfo(path.stem, path)
            for path in sorted(directory.glob("*.json"), key=lambda item: item.name.casefold())
            if path.is_file() and not path.is_symlink()
        )
    return books


def book_path(name: str, directory: Path = BOOKS_DIR) -> Path:
    if not isinstance(name, str):
        raise ValueError("请填写答案册名称")
    name = name.strip()
    while name.lower().endswith(".json"):
        name = name[:-5]
    if (not name or len(name) > 80 or name.endswith((".", " "))
            or re.search(r'[<>:"/\\|?*\x00-\x1f]', name)):
        raise ValueError("名称请使用 1–80 个字符，不能含 / \\ : * ? 等文件名禁用字符或以点结尾")
    reserved = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    reserved.update(f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10))
    if unicodedata.normalize("NFKC", name.split(".")[0].rstrip(" .")).upper() in reserved:
        raise ValueError("这个名称是 Windows 保留名称，请换一个")
    if name == DEFAULT_BOOK_LABEL:
        raise ValueError("这个名称留给脚本自带的默认答案册，请换一个")
    directory = Path(directory).resolve()
    target = directory / f"{name}.json"
    if target.resolve() == DEFAULT_BOOK_PATH.resolve():
        raise ValueError("脚本自带的默认答案册只读，请另建答案册")
    if target.resolve().parent != directory or target.is_symlink():
        raise ValueError("答案册必须保存在 answer_books 文件夹内，不能使用文件链接")
    return target


def save_book(name: str, chapters: Mapping[str, Any], *, overwrite: bool = False,
              directory: Path = BOOKS_DIR) -> Path:
    data = validate_chapter_book(chapters)
    path = book_path(name, directory)
    if path.exists() and not overwrite:
        raise FileExistsError(f"答案册“{path.stem}”已存在")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".answer-book-", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if overwrite:
            os.replace(temporary, path)
        elif os.name == "nt":
            # Windows rename is atomic and refuses to replace an existing file.
            os.rename(temporary, path)
        else:
            # A hard link provides the same no-clobber guarantee on POSIX.
            os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def choose_book(directory: Path = BOOKS_DIR) -> Path:
    books = list_books(directory)
    print("\n选择本次课程的答案册（不会自动识别课程，也不会记住上次选择）：")
    for index, book in enumerate(books):
        print(f"  {index}. {book.name}")
    print("新答案册请先用 edit_answers.cmd 录入。缺答案会暂停，不会改用默认答案。")
    while True:
        choice = input("输入编号，直接回车使用 0，输入 q 退出：").strip()
        if choice.lower() in {"q", "quit", "退出"}:
            raise KeyboardInterrupt
        if not choice:
            return books[0].path
        if choice.isascii() and choice.isdecimal() and int(choice) < len(books):
            return books[int(choice)].path
        print("没有这个编号，请重新选择；未切换答案册。")
