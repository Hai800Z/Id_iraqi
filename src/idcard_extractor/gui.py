"""Desktop interface (Tkinter): choose card photos, run the pipeline, open the results.

Start it with ``idcard-extract-gui``, ``python -m idcard_extractor.gui`` or run_gui.bat.
"""

from __future__ import annotations

import contextlib
import logging
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Optional

from dotenv import find_dotenv

from idcard_extractor.config import ConfigError, Settings, load_settings
from idcard_extractor.logging_utils import configure_logging
from idcard_extractor.pipeline import IMAGE_EXTENSIONS

log = logging.getLogger("idcard_extractor.gui")

TITLE = "قارئ البطاقة الوطنية العراقية"
FONT = ("Segoe UI", 11)
IMAGE_TYPES = [("صور", " ".join(f"*{ext}" for ext in sorted(IMAGE_EXTENSIONS))), ("كل الملفات", "*.*")]
MAX_LOG_LINES = 2000


def open_path(path: Path) -> None:
    """Open a file or folder with the system's default application."""
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606 (a path produced by this program)
    else:
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.Popen([opener, str(path)])  # noqa: S603


class _QueueHandler(logging.Handler):
    """Hand log records to the interface thread."""

    def __init__(self, events: queue.Queue):
        super().__init__()
        self.events = events

    def emit(self, record: logging.LogRecord) -> None:
        self.events.put(("log", self.format(record)))


class App:
    def __init__(self, root: tk.Tk, settings: Settings):
        self.root = root
        self.settings = settings
        self.inputs: list[Path] = []
        self.events: queue.Queue = queue.Queue()
        self.pipeline = None  # the models are loaded once and reused between runs
        self.running = False
        self.closed = False
        self.excel_path: Optional[Path] = None

        self.excel_var = tk.StringVar()
        self.db_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="أضف صور البطاقات ثم اضغط «ابدأ المعالجة».")
        self.summary_var = tk.StringVar()

        self._build()
        self._handler = _QueueHandler(self.events)
        self._handler.setFormatter(logging.Formatter("%(asctime)s  %(levelname)s  %(message)s", "%H:%M:%S"))
        logging.getLogger().addHandler(self._handler)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self._poll)

    # ------------------------------------------------------------------ layout

    def _build(self) -> None:
        root = self.root
        root.title(TITLE)
        root.geometry("900x700")
        root.minsize(760, 580)
        style = ttk.Style(root)
        style.configure(".", font=FONT)
        style.configure("Start.TButton", font=(FONT[0], 12, "bold"))

        main = ttk.Frame(root, padding=12)
        main.pack(fill="both", expand=True)

        photos = ttk.LabelFrame(main, text="صور البطاقات (الوجه والظهر بأي ترتيب)", padding=8, labelanchor="ne")
        photos.pack(fill="x")
        buttons = ttk.Frame(photos)
        buttons.pack(side="right", fill="y", padx=(8, 0))
        for text, command in (("إضافة صور…", self.choose_files), ("إضافة مجلد…", self.choose_folder),
                              ("إزالة المحدد", self.remove_selected), ("مسح الكل", self.clear_inputs)):
            ttk.Button(buttons, text=text, command=command).pack(fill="x", pady=2)
        scroll = ttk.Scrollbar(photos, orient="vertical")
        self.listbox = tk.Listbox(photos, height=7, selectmode="extended", font=(FONT[0], 10),
                                  yscrollcommand=scroll.set)
        scroll.configure(command=self.listbox.yview)
        scroll.pack(side="left", fill="y")
        self.listbox.pack(side="left", fill="both", expand=True)

        output = ttk.LabelFrame(main, text="النتائج", padding=8, labelanchor="ne")
        output.pack(fill="x", pady=(10, 0))
        row = ttk.Frame(output)
        row.pack(fill="x")
        ttk.Label(row, text="ملف Excel:").pack(side="right")
        ttk.Button(row, text="اختيار…", command=self.choose_excel).pack(side="left")
        ttk.Entry(row, textvariable=self.excel_var).pack(side="right", fill="x", expand=True, padx=6)
        ttk.Label(output, text="اتركه فارغاً ليُحفظ تلقائياً في مجلد output",
                  foreground="gray").pack(anchor="e")
        db_row = ttk.Frame(output)
        db_row.pack(fill="x", pady=(6, 0))
        ttk.Button(db_row, text="فحص قاعدة البيانات", command=self.check_database).pack(side="left")
        ttk.Checkbutton(db_row, text="حفظ البطاقات المقبولة في قاعدة البيانات",
                        variable=self.db_var).pack(side="right")

        run = ttk.Frame(main, padding=(0, 12, 0, 4))
        run.pack(fill="x")
        self.start_button = ttk.Button(run, text="ابدأ المعالجة", style="Start.TButton", command=self.start)
        self.start_button.pack(side="right", ipadx=14, ipady=4)
        self.progress = ttk.Progressbar(run, mode="determinate")
        self.progress.pack(side="right", fill="x", expand=True, padx=(0, 10))
        ttk.Label(main, textvariable=self.status_var, anchor="e").pack(fill="x")

        summary = ttk.Frame(main)
        summary.pack(fill="x", pady=(6, 0))
        ttk.Button(summary, text="فتح مجلد النتائج", command=self.open_output_folder).pack(side="left")
        self.open_excel_button = ttk.Button(summary, text="فتح ملف Excel", command=self.open_excel,
                                            state="disabled")
        self.open_excel_button.pack(side="left", padx=6)
        ttk.Label(summary, textvariable=self.summary_var, font=(FONT[0], 11, "bold"),
                  anchor="e").pack(side="right", fill="x", expand=True)

        log_frame = ttk.LabelFrame(main, text="السجل", padding=6, labelanchor="ne")
        log_frame.pack(fill="both", expand=True, pady=(10, 0))
        self.log_text = scrolledtext.ScrolledText(log_frame, height=10, state="disabled",
                                                  font=("Consolas", 9), wrap="word")
        self.log_text.pack(fill="both", expand=True)

    # ------------------------------------------------------------------ inputs

    def add_paths(self, paths) -> None:
        for path in map(Path, paths):
            if path not in self.inputs:
                self.inputs.append(path)
                self.listbox.insert("end", str(path))
        if not self.running:
            self.status_var.set(f"عدد المدخلات: {len(self.inputs)}")

    def choose_files(self) -> None:
        self.add_paths(filedialog.askopenfilenames(title="اختر صور البطاقات", filetypes=IMAGE_TYPES))

    def choose_folder(self) -> None:
        folder = filedialog.askdirectory(title="اختر مجلد الصور")
        if folder:
            self.add_paths([folder])

    def remove_selected(self) -> None:
        for index in reversed(self.listbox.curselection()):
            self.listbox.delete(index)
            del self.inputs[index]

    def clear_inputs(self) -> None:
        self.listbox.delete(0, "end")
        self.inputs.clear()

    def choose_excel(self) -> None:
        path = filedialog.asksaveasfilename(
            title="حفظ النتائج باسم",
            defaultextension=".xlsx",
            filetypes=[("Excel", "*.xlsx")],
            initialdir=str(self.settings.output_dir),
            initialfile=f"cards_{datetime.now():%Y%m%d_%H%M%S}.xlsx",
        )
        if path:
            self.excel_var.set(path)

    # --------------------------------------------------------------------- run

    def start(self) -> None:
        if self.running:
            return
        if not self.inputs:
            messagebox.showwarning(TITLE, "أضف صورة أو مجلداً أولاً.")
            return
        excel = self.excel_var.get().strip()
        excel_path = Path(excel) if excel else (
            self.settings.output_dir / f"cards_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
        )
        self._set_running(True)
        self.progress.configure(value=0, maximum=1)
        self.summary_var.set("")
        threading.Thread(target=self._work, args=(list(self.inputs), excel_path, self.db_var.get()),
                         daemon=True).start()

    def _work(self, inputs: list[Path], excel_path: Path, use_db: bool) -> None:
        """Runs in a background thread; talks to the window only through ``self.events``."""
        from idcard_extractor.db.mapping import record_context
        from idcard_extractor.exporters.excel import export_to_excel
        from idcard_extractor.pipeline import CardPipeline, collect_images

        try:
            images = collect_images(inputs)
            if not images:
                self.events.put(("error", "لم يُعثر على صور في المدخلات المختارة."))
                return

            writer, db_problem = None, None
            if use_db:
                from idcard_extractor.db.setup import DatabaseSetupError, prepare_writer

                try:
                    writer = prepare_writer(self.settings, None, self.settings.db_failed_records_file)
                except DatabaseSetupError as exc:
                    log.error("%s", exc)
                    db_problem = "تعذّر استخدام قاعدة البيانات (راجع السجل)، فحُفظ ملف Excel فقط."

            if self.pipeline is None:
                self.events.put(("status", "جارٍ تحميل النماذج… قد يستغرق ذلك دقيقة في المرة الأولى."))
                pipeline = CardPipeline(self.settings)
                pipeline.load_models()
                self.pipeline = pipeline

            self.events.put(("status", f"جارٍ معالجة {len(images)} صورة…"))
            result = self.pipeline.run(
                images, progress=lambda done, total, path: self.events.put(("progress", done, total, path.name))
            )
            try:
                export_to_excel(result.records, excel_path, result.rejected)
            except PermissionError:
                self.events.put(("error", f"تعذّر حفظ {excel_path.name}. أغلقه إن كان مفتوحاً ثم أعد المحاولة."))
                return

            report = None
            if writer is not None:
                processed_at = datetime.now().replace(microsecond=0)
                report = writer.write(record_context(card, processed_at) for card in result.accepted)
            self.events.put(("done", result, excel_path, report, db_problem))
        except Exception as exc:
            log.exception("Processing failed")
            self.events.put(("error", f"حدث خطأ أثناء المعالجة: {type(exc).__name__}: {exc}"))

    def check_database(self) -> None:
        def work() -> None:
            from sqlalchemy.exc import SQLAlchemyError

            from idcard_extractor.db.setup import DatabaseSetupError, open_database
            from idcard_extractor.db.writer import DatabaseWriter, describe_db_error

            try:
                engine, mapping = open_database(self.settings)
                issues = DatabaseWriter(engine, mapping).check()
            except DatabaseSetupError as exc:
                self.events.put(("db_check", False, str(exc)))
                return
            except SQLAlchemyError as exc:
                self.events.put(("db_check", False, f"Cannot reach the database: {describe_db_error(exc)}"))
                return
            ok = not any(issue.level == "error" for issue in issues)
            head = f"الجدول {mapping.qualified_table}: " + ("جاهز للكتابة." if ok else "توجد أخطاء يجب إصلاحها.")
            lines = [f"{issue.level.upper()}: {issue.message}" for issue in issues]
            self.events.put(("db_check", ok, "\n".join([head, *lines])))

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------ event loop

    def _poll(self) -> None:
        try:
            while True:
                self._handle(self.events.get_nowait())
        except queue.Empty:
            pass
        if not self.closed:
            self.root.after(100, self._poll)

    def _handle(self, event: tuple) -> None:
        kind = event[0]
        if kind == "log":
            self._append_log(event[1])
        elif kind == "status":
            self.status_var.set(event[1])
        elif kind == "progress":
            _, done, total, name = event
            self.progress.configure(maximum=total, value=done)
            self.status_var.set(f"تمت معالجة {done} من {total}: {name}")
        elif kind == "done":
            _, result, excel_path, report, db_problem = event
            self._set_running(False)
            self.excel_path = excel_path
            self.open_excel_button.configure(state="normal")
            text = f"المقبولة: {len(result.accepted)}    المرفوضة: {len(result.rejected)}"
            if report is not None:
                text += (f"    قاعدة البيانات: أُضيف {report.inserted}، حُدّث {report.updated}، "
                         f"تُخطّي {report.skipped}، فشل {report.failed}")
            self.summary_var.set(text)
            self.status_var.set(f"اكتملت المعالجة، وحُفظت النتائج في {excel_path}")
            if db_problem:
                messagebox.showwarning(TITLE, db_problem)
            elif report is not None and not report.ok:
                messagebox.showwarning(TITLE, "لم تُحفظ بعض البطاقات في قاعدة البيانات؛ السبب في السجل.")
        elif kind == "error":
            self._set_running(False)
            self.status_var.set(event[1])
            messagebox.showerror(TITLE, event[1])
        elif kind == "db_check":
            _, ok, message = event
            (messagebox.showinfo if ok else messagebox.showerror)(TITLE, message)

    def _append_log(self, line: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line + "\n")
        overflow = int(self.log_text.index("end-1c").split(".")[0]) - MAX_LOG_LINES
        if overflow > 0:
            self.log_text.delete("1.0", f"{overflow + 1}.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _set_running(self, running: bool) -> None:
        self.running = running
        self.start_button.configure(state="disabled" if running else "normal")

    # ----------------------------------------------------------------- results

    def open_excel(self) -> None:
        if self.excel_path and self.excel_path.is_file():
            open_path(self.excel_path)

    def open_output_folder(self) -> None:
        folder = self.excel_path.parent if self.excel_path else self.settings.output_dir
        folder.mkdir(parents=True, exist_ok=True)
        open_path(folder)

    def close(self) -> None:
        if self.running and not messagebox.askyesno(TITLE, "المعالجة جارية. هل تريد الإغلاق؟"):
            return
        self.closed = True
        logging.getLogger().removeHandler(self._handler)
        self.root.destroy()


def _enter_project_folder() -> None:
    """Started from a shortcut elsewhere: use the project folder, so that .env and
    the relative paths in it (weights, output) are found."""
    if find_dotenv(usecwd=True):
        return
    root = Path(__file__).resolve().parents[2]
    if (root / "pyproject.toml").is_file():
        os.chdir(root)


def main() -> int:
    # pythonw has no console; libraries that print must not crash.
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))  # noqa: SIM115
    if sys.platform == "win32":
        import ctypes

        with contextlib.suppress(AttributeError, OSError):  # sharp text on high-DPI screens
            ctypes.windll.shcore.SetProcessDpiAwareness(1)

    _enter_project_folder()
    root = tk.Tk()
    try:
        settings = load_settings()
    except ConfigError as exc:
        messagebox.showerror(TITLE, f"خطأ في الإعدادات: {exc}")
        root.destroy()
        return 1
    configure_logging(settings.log_level, settings.log_file, settings.mask_pii)
    App(root, settings)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
