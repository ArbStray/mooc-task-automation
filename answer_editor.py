"""中文答案册编辑器。仅使用 Python 标准库，不启动课程浏览器。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from answer_books import (
    DEFAULT_BOOK_LABEL,
    BookInfo,
    book_path,
    chapter_groups,
    format_book_text,
    list_books,
    load_chapter_book,
    parse_book_text,
    save_book,
)


class AnswerBookEditor:
    def __init__(self, window, tk, ttk, messagebox):
        self.window = window
        self.tk = tk
        self.messagebox = messagebox
        self.books: list[BookInfo] = []
        self.current_book: BookInfo | None = None
        self.snapshot = ("", "")
        self.loading = True
        self.book_var = tk.StringVar()
        self.name_var = tk.StringVar()
        self.status_var = tk.StringVar()

        window.title("课程答案册管理")
        window.geometry("900x690")
        window.minsize(760, 570)
        window.protocol("WM_DELETE_WINDOW", self.close)

        frame = ttk.Frame(window, padding=16)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(4, weight=1)
        ttk.Label(frame, text="整门课程的练习答案册", font=("Microsoft YaHei UI", 15)).grid(
            row=0, column=0, sticky="w", pady=(0, 12)
        )

        toolbar = ttk.Frame(frame)
        toolbar.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        toolbar.columnconfigure(1, weight=1)
        ttk.Label(toolbar, text="选择答案册：").grid(row=0, column=0)
        self.book_combo = ttk.Combobox(toolbar, textvariable=self.book_var, state="readonly")
        self.book_combo.grid(row=0, column=1, sticky="ew", padx=(0, 8))
        self.book_combo.bind("<<ComboboxSelected>>", self.select_book)
        ttk.Button(toolbar, text="新建空白答案册", command=self.new_book).grid(row=0, column=2)
        ttk.Button(toolbar, text="刷新列表", command=self.refresh_books).grid(
            row=0, column=3, padx=(8, 0)
        )

        name_frame = ttk.Frame(frame)
        name_frame.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        name_frame.columnconfigure(1, weight=1)
        ttk.Label(name_frame, text="答案册名称：").grid(row=0, column=0)
        self.name_entry = ttk.Entry(name_frame, textvariable=self.name_var)
        self.name_entry.grid(row=0, column=1, sticky="ew")
        ttk.Label(name_frame, text="修改名称会另存一本，原册保留。").grid(
            row=1, column=1, sticky="w", pady=(4, 0)
        )

        ttk.Label(
            frame,
            text=(
                "每行填写一个练习章节：章节号 = 按题号顺序排列的答案\n"
                "例如：1.1.2 = A(BC)D，表示第 1 题选 A、第 2 题选 BC、第 3 题选 D。\n"
                "只填写练习所在章节；各册的章节位置可以不同。新建时不会复制默认答案。"
            ),
            justify="left",
            wraplength=840,
        ).grid(row=3, column=0, sticky="w", pady=(0, 10))

        text_frame = ttk.Frame(frame)
        text_frame.grid(row=4, column=0, sticky="nsew")
        text_frame.columnconfigure(0, weight=1)
        text_frame.rowconfigure(0, weight=1)
        self.answer_text = tk.Text(
            text_frame, wrap="none", undo=True, maxundo=50, font=("Consolas", 12),
            width=72, height=14, padx=8, pady=8,
        )
        self.answer_text.grid(row=0, column=0, sticky="nsew")
        yscroll = ttk.Scrollbar(text_frame, orient="vertical", command=self.answer_text.yview)
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll = ttk.Scrollbar(text_frame, orient="horizontal", command=self.answer_text.xview)
        xscroll.grid(row=1, column=0, sticky="ew")
        self.answer_text.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.answer_text.bind("<<Modified>>", self.text_changed)

        ttk.Label(frame, textvariable=self.status_var, wraplength=840).grid(
            row=5, column=0, sticky="w", pady=(8, 10)
        )
        actions = ttk.Frame(frame)
        actions.grid(row=6, column=0, sticky="e")
        ttk.Button(actions, text="检查格式 / 题数", command=self.check_answers).pack(
            side="left", padx=(0, 8)
        )
        self.save_button = ttk.Button(actions, text="保存答案册", command=self.save)
        self.save_button.pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="关闭", command=self.close).pack(side="left")
        ttk.Label(
            frame,
            text=(
                "保存只写入本地答案册，不会开始做题，也不会改变启动默认值。\n"
                "运行 start.cmd 时人工选择对应答案册；不切换仍使用脚本自带答案册。"
            ),
            justify="left",
            wraplength=840,
        ).grid(row=7, column=0, sticky="w", pady=(12, 0))

        self.name_var.trace_add("write", self.name_changed)
        window.bind("<Control-s>", self.save_shortcut)
        window.bind("<Control-n>", self.new_shortcut)
        self.books = list_books()
        self.update_choices()
        initial = self.books[0]
        self.show_book(initial, load_chapter_book(initial.path))

    @staticmethod
    def label(book: BookInfo) -> str:
        return DEFAULT_BOOK_LABEL if book.is_default else f"自建：{book.name}"

    def update_choices(self):
        self.book_combo.configure(values=[self.label(book) for book in self.books])
        self.restore_selection()

    def restore_selection(self):
        if self.current_book is None:
            self.book_var.set("新建答案册（尚未保存）")
            return
        for index, book in enumerate(self.books):
            if book.path == self.current_book.path:
                self.book_combo.current(index)
                return
        self.book_var.set(f"当前：{self.current_book.name}（文件未列出）")

    def draft(self) -> tuple[str, str]:
        return self.name_var.get(), self.answer_text.get("1.0", "end-1c")

    def is_readonly(self) -> bool:
        return self.current_book is not None and self.current_book.is_default

    def is_dirty(self) -> bool:
        return not self.is_readonly() and self.draft() != self.snapshot

    def show_book(self, book: BookInfo | None, chapters):
        self.loading = True
        self.current_book = book
        self.name_entry.configure(state="normal")
        self.name_var.set(book.name if book else "")
        self.answer_text.configure(state="normal")
        self.answer_text.delete("1.0", "end")
        self.answer_text.insert("1.0", format_book_text(chapters) if chapters else "")
        self.answer_text.edit_reset()
        self.answer_text.edit_modified(False)
        readonly = self.is_readonly()
        self.name_entry.configure(state="readonly" if readonly else "normal")
        self.answer_text.configure(state="disabled" if readonly else "normal")
        self.save_button.configure(state="disabled" if readonly else "normal")
        self.snapshot = self.draft()
        self.restore_selection()
        self.loading = False
        self.update_status()

    def name_changed(self, *_):
        if not self.loading:
            self.update_status()

    def text_changed(self, _event=None):
        if self.answer_text.edit_modified():
            self.answer_text.edit_modified(False)
            if not self.loading:
                self.update_status()

    def update_status(self):
        dirty = self.is_dirty()
        self.window.title("课程答案册管理" + (" * 未保存" if dirty else ""))
        prefix = "自带答案册只读" if self.is_readonly() else ("未保存" if dirty else "已保存")
        content = self.draft()[1]
        if not content.strip():
            self.status_var.set("空白答案册：请填写名称和练习答案后保存。")
            return
        try:
            chapters = parse_book_text(content)
        except ValueError as exc:
            self.status_var.set(f"{prefix}；格式待修正：{exc}")
            return
        questions = sum(len(chapter_groups(value)) for value in chapters.values())
        self.status_var.set(f"{prefix}；共 {len(chapters)} 个练习章节，{questions} 道题。")

    def check_answers(self):
        try:
            chapters = parse_book_text(self.draft()[1])
        except ValueError as exc:
            self.messagebox.showerror("答案格式有问题", str(exc), parent=self.window)
            return
        questions = sum(len(chapter_groups(value)) for value in chapters.values())
        self.messagebox.showinfo(
            "格式检查通过",
            f"共 {len(chapters)} 个练习章节，{questions} 道题。\n"
            "这里只检查格式；请自行确认章节位置、题号顺序和答案正确性。",
            parent=self.window,
        )

    def confirm_abandon(self) -> bool:
        if not self.is_dirty():
            return True
        choice = self.messagebox.askyesnocancel(
            "当前答案尚未保存",
            "是否先保存当前答案册？\n\n是：保存后继续。\n否：放弃本次编辑。\n取消：留在这里。",
            parent=self.window,
        )
        if choice is None:
            return False
        return self.save() if choice else True

    def select_book(self, _event=None):
        index = self.book_combo.current()
        if index < 0 or index >= len(self.books):
            self.restore_selection()
            return
        requested = self.books[index]
        self.restore_selection()
        if self.current_book and requested.path == self.current_book.path:
            return
        if not self.confirm_abandon():
            return
        try:
            chapters = load_chapter_book(requested.path)
        except (OSError, ValueError) as exc:
            self.messagebox.showerror(
                "无法打开答案册", f"{requested.path}\n\n{exc}\n\n当前内容仍保留。", parent=self.window
            )
            return
        self.show_book(requested, chapters)

    def new_book(self):
        if self.confirm_abandon():
            self.show_book(None, {})
            self.name_entry.focus_set()

    def refresh_books(self):
        try:
            books = list_books()
        except (OSError, ValueError) as exc:
            self.messagebox.showerror("无法刷新答案册列表", str(exc), parent=self.window)
            return
        self.books = books
        self.update_choices()

    def save(self) -> bool:
        if self.is_readonly():
            self.messagebox.showinfo(
                "自带答案册只读", "请点击“新建空白答案册”录入其他课程的答案。", parent=self.window
            )
            return False
        try:
            name, content = self.draft()
            chapters = parse_book_text(content)
            target = book_path(name)
            overwrite = target.exists()
            if overwrite and not self.messagebox.askyesno(
                "确认覆盖答案册",
                f"“{target.stem}”已经存在，是否用当前内容覆盖？\n\n{target}",
                parent=self.window,
            ):
                return False
            saved_path = save_book(name, chapters, overwrite=overwrite)
        except (OSError, ValueError) as exc:
            self.messagebox.showerror(
                "答案册未保存", f"{exc}\n\n当前编辑内容仍保留，请修正后重试。", parent=self.window
            )
            return False

        saved = BookInfo(name=Path(saved_path).stem, path=Path(saved_path))
        refresh_error = None
        try:
            self.books = list_books()
        except (OSError, ValueError) as exc:
            refresh_error = exc
            self.books = [book for book in self.books if book.path != saved.path] + [saved]
        self.update_choices()
        self.show_book(saved, chapters)
        if refresh_error is not None:
            self.messagebox.showwarning(
                "答案已保存，列表未刷新",
                f"已保存到：{saved.path}\n\n刷新列表失败：{refresh_error}",
                parent=self.window,
            )
        else:
            self.messagebox.showinfo(
                "答案册已保存",
                f"已保存到：{saved.path}\n\n运行 start.cmd 时选择这本答案册即可。\n"
                "本次保存不会改变启动时的默认答案册。",
                parent=self.window,
            )
        return True

    def close(self):
        if self.confirm_abandon():
            self.window.destroy()

    def save_shortcut(self, _event=None):
        self.save()
        return "break"

    def new_shortcut(self, _event=None):
        self.new_book()
        return "break"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="手动录入并管理整门课程的练习答案册。")
    parser.parse_args(argv)
    try:
        import tkinter as tk
        from tkinter import messagebox, ttk
    except ImportError:
        print(
            "无法启动答案册编辑器：当前 Python 没有 tkinter。\n"
            "请使用 Python 官方 Windows 安装程序，启用“tcl/tk and IDLE”，然后重试。\n"
            "不要使用 pip 安装 tkinter。",
            file=sys.stderr,
        )
        return 1
    try:
        window = tk.Tk()
    except tk.TclError as exc:
        print(f"无法打开答案册编辑窗口：{exc}", file=sys.stderr)
        return 1
    try:
        AnswerBookEditor(window, tk, ttk, messagebox)
    except (OSError, ValueError) as exc:
        messagebox.showerror("无法读取答案册", str(exc), parent=window)
        window.destroy()
        return 1
    window.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
