#!/usr/bin/env python3
"""Настольный интерфейс мониторинга. Запуск: python construction_app.py."""
from __future__ import annotations

import json
import math
import queue
import tempfile
import threading
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from construction_monitor import (
    analyze,
    load_config,
    normalize,
    parse_equipment,
    resolve_work,
    run_detector,
    summarize_result,
    write_html,
)
from yolo_detector import detect_counts

try:
    from PIL import Image, ImageOps, ImageTk
except ImportError:  # The analysis and HTML report work without Pillow.
    Image = ImageOps = ImageTk = None


BG = "#eef2f5"
WHITE = "#ffffff"
INK = "#1c2c3d"
MUTED = "#536577"
BORDER = "#dce4ec"
BLUE = "#3d648d"
GREEN = "#16705b"
AMBER = "#a36310"
FONT = ("Segoe UI", 10)


@dataclass(frozen=True)
class AnalysisRequest:
    work_id: str
    image: Path
    detector_script: Path | None = None
    model_path: Path | None = None
    confidence: float = 0.3
    no_haul: bool = False
    visibility: str = "full"
    idle: str = ""
    timeout: int = 120

def analyze_snapshot(request: AnalysisRequest, config: dict) -> dict:
    """The GUI and CLI use the same detector contract and decision rules."""
    if (request.model_path is None) == (request.detector_script is None):
        raise ValueError("Укажите одну модель YOLO или один внешний Python-детектор.")
    work = resolve_work(request.work_id, config)
    idle = parse_equipment(request.idle, config)
    if request.model_path is not None:
        counts = detect_counts(request.image, request.model_path,
                               confidence=request.confidence)
    else:
        counts = run_detector(request.detector_script, request.image, config,
                              timeout=request.timeout)
    result = analyze(work, counts, config, input_mode="detector",
                     scope="whole_site", visibility=request.visibility,
                     idle=idle, no_haul=request.no_haul)
    result["source_image"] = str(request.image.expanduser().resolve())
    result["detector_backend"] = "yolo" if request.model_path is not None else "external_script"
    if request.model_path is not None:
        result["model_path"] = str(request.model_path.expanduser().resolve())
        result["confidence_threshold"] = request.confidence
    return result

class MonitorApp:
    def __init__(self, root: tk.Tk, config: dict):
        self.root = root
        self.config = config
        self.result: dict | None = None
        self.selected_work: dict | None = None
        self.visible_works: list[dict] = []
        self.responses: queue.Queue = queue.Queue()
        self.busy = False
        self.preview_image = None
        self._preserve_work_selection = False
        self.temp_reports = tempfile.TemporaryDirectory(prefix="construction_reports_")

        root.title("СтройМонитор — сопоставление снимка с планом")
        root.geometry("1170x800")
        root.minsize(990, 740)
        root.configure(bg=BG)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._configure_style()
        self._build_layout()
        self._filter_works()

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("App.TEntry", padding=7, font=FONT)
        style.configure("App.TButton", padding=(11, 7), font=FONT)
        style.configure("Primary.TButton", padding=(14, 10), font=("Segoe UI", 10, "bold"),
                        foreground=WHITE, background=BLUE)
        style.map("Primary.TButton", background=[("active", "#304f70"),
                                                  ("disabled", "#aab8c5")])

    def _build_layout(self) -> None:
        shell = tk.Frame(self.root, bg=BG)
        shell.pack(fill="both", expand=True, padx=20, pady=16)

        header = tk.Frame(shell, bg=BG)
        header.pack(fill="x", pady=(0, 15))
        tk.Label(header, text="СТРОЙМОНИТОР", bg=BG, fg=BLUE,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w")
        tk.Label(header, text="Снимок площадки и план работ", bg=BG, fg=INK,
                 font=("Segoe UI", 22, "bold")).pack(anchor="w", pady=(2, 0))

        workspace = tk.Frame(shell, bg=BG)
        workspace.pack(fill="both", expand=True)
        sidebar = tk.Frame(workspace, bg=WHITE, width=365, highlightbackground=BORDER,
                           highlightthickness=1)
        sidebar.pack(side="left", fill="y", padx=(0, 16))
        sidebar.pack_propagate(False)
        self._build_inputs(sidebar)

        right = tk.Frame(workspace, bg=WHITE, highlightbackground=BORDER,
                         highlightthickness=1)
        right.pack(side="left", fill="both", expand=True)
        self._build_report_area(right)

    def _section(self, parent: tk.Widget, title: str) -> None:
        tk.Label(parent, text=title, bg=WHITE, fg=INK,
                 font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(16, 7))

    def _build_inputs(self, panel: tk.Frame) -> None:
        body = tk.Frame(panel, bg=WHITE)
        body.pack(fill="both", expand=True, padx=18, pady=(5, 10))
        tk.Label(body, text="НАСТРОЙКА АНАЛИЗА", bg=WHITE, fg=MUTED,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(8, 1))

        self._section(body, "1. Плановая работа")
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._filter_works())
        search = ttk.Entry(body, textvariable=self.search_var, style="App.TEntry")
        search.pack(fill="x")
        search.bind("<Return>", lambda _: self._select_first())
        tk.Label(body, text="Введите код R047, 12.3.1 или часть названия; можно кликнуть строку ниже",
                 bg=WHITE, fg=MUTED, font=("Segoe UI", 9)).pack(anchor="w", pady=(4, 6))
        self.work_list = tk.Listbox(body, height=6, relief="flat", borderwidth=1,
                                    highlightthickness=1, highlightcolor=BLUE,
                                    highlightbackground=BORDER, selectbackground=BLUE,
                                    selectforeground=WHITE, font=("Segoe UI", 9))
        self.work_list.pack(fill="x")
        self.work_list.bind("<<ListboxSelect>>", self._choose_work)
        self.work_hint = tk.Label(body, text="Выберите строку из списка", bg=WHITE,
                                  fg=MUTED, font=("Segoe UI", 9), wraplength=325,
                                  justify="left")
        self.work_hint.pack(anchor="w", pady=(7, 0))

        self._section(body, "2. Снимок всей площадки")
        self.image_var = tk.StringVar()
        self._path_picker(body, self.image_var, "Снимок...", self._browse_image)

        self._section(body, "3. Веса YOLO")
        default_model = Path(__file__).resolve().parent / "runs/segment/train-6/weights/best.pt"
        self.model_var = tk.StringVar(value=str(default_model) if default_model.is_file() else "")
        source_row = tk.Frame(body, bg=WHITE)
        source_row.pack(fill="x")
        source_row.columnconfigure(0, weight=1)
        self.source_entry = ttk.Entry(source_row, textvariable=self.model_var, style="App.TEntry")
        self.source_entry.grid(row=0, column=0, sticky="ew")
        self.source_button = ttk.Button(source_row, text="Выбрать .pt...",
                                        style="App.TButton", command=self._browse_model)
        self.source_button.grid(row=0, column=1, padx=(6, 0))
        self.source_hint = tk.Label(body, text="Укажите путь к файлу best.pt вашей обученной модели.",
                                    bg=WHITE, fg=MUTED, font=("Segoe UI", 9),
                                    wraplength=325, justify="left")
        self.source_hint.pack(anchor="w", pady=(5, 0))

        self._section(body, "Параметры YOLO")
        options = tk.Frame(body, bg=WHITE)
        options.pack(fill="x", pady=(0, 0))
        tk.Label(options, text="Порог уверенности:", bg=WHITE, fg=MUTED,
                 font=("Segoe UI", 9)).grid(row=0, column=0, sticky="w")
        self.confidence_var = tk.StringVar(value="0.3")
        self.confidence_entry = ttk.Entry(options, textvariable=self.confidence_var,
                                          width=8, style="App.TEntry")
        self.confidence_entry.grid(row=1, column=0, sticky="w", pady=(3, 0))

        controls = tk.Frame(body, bg=WHITE)
        controls.pack(fill="x", pady=(18, 0))
        self.run_button = ttk.Button(controls, text="Анализировать снимок",
                                     style="Primary.TButton", command=self._start_analysis)
        self.run_button.pack(fill="x")
        self.activity = tk.Label(controls, text="Готово к анализу", bg=WHITE, fg=MUTED,
                                 font=("Segoe UI", 9))
        self.activity.pack(anchor="w", pady=(7, 0))

    def _path_picker(self, parent, variable, button_text, command) -> None:
        row = tk.Frame(parent, bg=WHITE)
        row.pack(fill="x")
        row.columnconfigure(0, weight=1)
        ttk.Entry(row, textvariable=variable, style="App.TEntry").grid(
            row=0, column=0, sticky="ew")
        ttk.Button(row, text=button_text, style="App.TButton",
                   command=command).grid(row=0, column=1, padx=(6, 0))

    def _build_report_area(self, panel: tk.Frame) -> None:
        canvas = tk.Canvas(panel, bg=WHITE, highlightthickness=0)
        scrollbar = ttk.Scrollbar(panel, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="top", fill="both", expand=True)
        self.report_body = tk.Frame(canvas, bg=WHITE)
        window = canvas.create_window((0, 0), window=self.report_body, anchor="nw")
        self.report_body.bind("<Configure>", lambda _: canvas.configure(
            scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        self.report_canvas = canvas

        footer = tk.Frame(panel, bg=WHITE)
        footer.pack(fill="x", side="bottom", padx=22, pady=14)
        self.open_button = ttk.Button(footer, text="Открыть полный отчёт",
                                      style="App.TButton", command=self._open_report,
                                      state="disabled")
        self.open_button.pack(side="left")
        self.save_button = ttk.Button(footer, text="Сохранить HTML",
                                      style="App.TButton", command=self._save_report,
                                      state="disabled")
        self.save_button.pack(side="left", padx=8)
        self.json_button = ttk.Button(footer, text="Сохранить JSON",
                                      style="App.TButton", command=self._save_json,
                                      state="disabled")
        self.json_button.pack(side="left")
        self._placeholder()

    def _clear_report(self) -> None:
        for widget in self.report_body.winfo_children():
            widget.destroy()
        self.preview_image = None
        self.report_canvas.yview_moveto(0)

    def _placeholder(self) -> None:
        self._clear_report()
        box = tk.Frame(self.report_body, bg=WHITE)
        box.pack(fill="both", expand=True, padx=28, pady=70)
        tk.Label(box, text="Отчёт появится здесь", bg=WHITE, fg=INK,
                 font=("Segoe UI", 17, "bold")).pack(anchor="center")
        tk.Label(box, text="Выберите работу, снимок и детектор, затем запустите анализ.",
                 bg=WHITE, fg=MUTED, font=FONT, wraplength=500).pack(pady=12)

    def _filter_works(self) -> None:
        if not hasattr(self, "work_list"):
            return
        if self._preserve_work_selection:
            return
        self.selected_work = None
        self.work_hint.configure(text="Выберите строку из списка", fg=MUTED)
        query = normalize(self.search_var.get())
        matches = [work for work in self.config["works"] if not query or query in normalize(
            " ".join(str(work.get(k, "")) for k in
                     ("work_id", "source_code", "name", "path")))]
        self.visible_works = matches[:60]
        self.work_list.delete(0, "end")
        for work in self.visible_works:
            self.work_list.insert("end", f"{work['work_id']} · {work['source_code'] or '—'} · {work['name']}")
        if not matches:
            self.work_hint.configure(text="Работа не найдена. Уточните запрос.")
        elif len(matches) > 60:
            self.work_hint.configure(text=f"Показано 60 из {len(matches)}. Уточните поиск.")

    def _select_first(self) -> None:
        if self.visible_works:
            self.work_list.selection_clear(0, "end")
            self.work_list.selection_set(0)
            self._choose_work()

    def _choose_work(self, _event=None) -> None:
        selection = self.work_list.curselection()
        if not selection:
            return
        self.selected_work = self.visible_works[selection[0]]
        work = self.selected_work
        self._preserve_work_selection = True
        try:
            self.search_var.set(work["source_code"] or work["work_id"])
        finally:
            self._preserve_work_selection = False
        self.work_hint.configure(text=f"Выбрано: {work['work_id']} — {work['name']}", fg=GREEN)

    def _browse_image(self) -> None:
        path = filedialog.askopenfilename(title="Выберите снимок площадки",
                                          filetypes=[("Изображения", "*.jpg *.jpeg *.png *.webp *.bmp"),
                                                     ("Все файлы", "*.*")])
        if path:
            self.image_var.set(path)

    def _browse_model(self) -> None:
        title, filetypes = "Выберите веса модели YOLO", [("Веса YOLO", "*.pt"),
                                                          ("Все файлы", "*.*")]
        path = filedialog.askopenfilename(title=title, filetypes=filetypes)
        if path:
            self.model_var.set(path)

    def _start_analysis(self) -> None:
        if self.busy:
            return
        if self.selected_work is None:
            messagebox.showwarning("Не выбрана работа", "Выберите конкретную плановую работу из списка.")
            return
        selected_source = self.model_var.get().strip()
        if not self.image_var.get().strip() or not selected_source:
            messagebox.showwarning("Не выбраны файлы", "Укажите снимок и путь к весам YOLO (.pt).")
            return
        try:
            confidence = float(self.confidence_var.get())
            if not math.isfinite(confidence) or not 0 <= confidence <= 1:
                raise ValueError
        except ValueError:
            messagebox.showwarning("Неверный порог", "Укажите порог уверенности от 0 до 1.")
            return
        if Path(selected_source).suffix.lower() != ".pt":
            messagebox.showwarning("Неверный файл", "Укажите файл весов YOLO с расширением .pt.")
            return
        request = AnalysisRequest(self.selected_work["work_id"],
                                  Path(self.image_var.get().strip()),
                                  model_path=Path(selected_source),
                                  confidence=confidence)
        self.result = None
        self._placeholder()
        for button in (self.open_button, self.save_button, self.json_button):
            button.configure(state="disabled")
        self.busy = True
        self.run_button.configure(state="disabled")
        self.activity.configure(text="Детектор обрабатывает снимок…", fg=BLUE)
        threading.Thread(target=self._worker, args=(request,), daemon=True).start()
        self.root.after(100, self._poll_worker)

    def _worker(self, request: AnalysisRequest) -> None:
        try:
            result = analyze_snapshot(request, self.config)
            self.responses.put((result, None))
        except Exception as exc:
            self.responses.put((None, exc))

    def _poll_worker(self) -> None:
        try:
            result, error = self.responses.get_nowait()
        except queue.Empty:
            self.root.after(100, self._poll_worker)
            return
        self.busy = False
        self.run_button.configure(state="normal")
        if error is not None:
            self.activity.configure(text="Не удалось выполнить анализ", fg=AMBER)
            messagebox.showerror("Ошибка анализа", str(error))
            return
        self.result = result
        self.activity.configure(text="Анализ завершён", fg=GREEN)
        self._show_result(result)
        for button in (self.open_button, self.save_button, self.json_button):
            button.configure(state="normal")

    def _text(self, parent, value: str, *, size=10, bold=False, color=INK,
              pady=(0, 0)) -> tk.Label:
        label = tk.Label(parent, text=value, bg=parent.cget("bg"), fg=color,
                         font=("Segoe UI", size, "bold" if bold else "normal"),
                         justify="left", anchor="w", wraplength=490)
        label.pack(fill="x", pady=pady)
        return label

    def _show_result(self, result: dict) -> None:
        self._clear_report()
        content = tk.Frame(self.report_body, bg=WHITE)
        content.pack(fill="both", expand=True, padx=24, pady=22)
        self._text(content, "ОТЧЁТ ПО СНИМКУ", size=9, bold=True, color=BLUE)
        work = result["work"]
        self._text(content, f"{work['work_id']} / {work['source_code'] or 'без кода'} — {work['name']}",
                   size=16, bold=True, pady=(5, 2))
        self._text(content, result["class_name"], color=MUTED, pady=(0, 15))
        if result.get("detector_backend") == "yolo":
            self._text(content, f"Модель: {Path(result['model_path']).name} · порог {result['confidence_threshold']:.2f}",
                       size=9, color=MUTED, pady=(0, 12))

        compatible = result["status"] == "compatible"
        summary = summarize_result(result, self.config)
        status_color = GREEN if compatible else AMBER
        status_bg = "#e8f5ef" if compatible else "#fdf2e4"
        status = tk.Frame(content, bg=status_bg, highlightbackground=status_color,
                          highlightthickness=1)
        status.pack(fill="x", pady=(0, 17))
        status_body = tk.Frame(status, bg=status_bg)
        status_body.pack(fill="x", padx=15, pady=12)
        self._text(status_body, "РЕШЕНИЕ", size=9, bold=True, color=status_color)
        self._text(status_body, summary["decision"],
                   size=17, bold=True, color=status_color, pady=(3, 6))
        self._text(status_body, "Причина: " + summary["cause"], pady=(0, 6))
        self._text(status_body, summary["profile"], pady=(0, 6))
        self._text(status_body, "Действие: " + summary["action"], bold=True)

        self._text(content, "Наблюдаемая техника", size=13, bold=True, pady=(0, 8))
        equipment = ", ".join(f"{self.config['machines'][key]['name']} × {count}"
                               for key, count in result["counts"].items()) or "Техника не найдена"
        self._text(content, equipment, pady=(0, 17))

        self._text(content, "Снимок площадки", size=13, bold=True, pady=(0, 8))
        self._show_image(content, Path(result["source_image"]))

    def _show_image(self, parent: tk.Widget, path: Path) -> None:
        try:
            if Image is None:
                self.preview_image = tk.PhotoImage(file=str(path))
            else:
                with Image.open(path) as original:
                    preview = ImageOps.exif_transpose(original)
                    preview.thumbnail((510, 290))
                    self.preview_image = ImageTk.PhotoImage(preview.copy())
            tk.Label(parent, image=self.preview_image, bg=WHITE).pack(anchor="w")
        except (OSError, tk.TclError):
            self._text(parent, "Предпросмотр недоступен. Снимок будет в HTML-отчёте.",
                       color=MUTED)

    def _report_name(self) -> str:
        return f"report_{self.result['work']['work_id']}_{datetime.now():%Y%m%d_%H%M%S}.html"

    def _open_report(self) -> None:
        if self.result is None:
            return
        try:
            path = Path(self.temp_reports.name) / self._report_name()
            write_html(self.result, path, self.config, embed_image=True)
            webbrowser.open(path.resolve().as_uri())
        except (OSError, ValueError) as exc:
            messagebox.showerror("Не удалось открыть отчёт", str(exc))

    def _save_report(self) -> None:
        if self.result is None:
            return
        name = filedialog.asksaveasfilename(title="Сохранить HTML-отчёт",
                                            initialfile=self._report_name(),
                                            defaultextension=".html",
                                            filetypes=[("HTML", "*.html")])
        if name:
            try:
                write_html(self.result, name, self.config, embed_image=True)
                self.activity.configure(text="HTML-отчёт сохранён", fg=GREEN)
            except (OSError, ValueError) as exc:
                messagebox.showerror("Не удалось сохранить отчёт", str(exc))

    def _save_json(self) -> None:
        if self.result is None:
            return
        name = filedialog.asksaveasfilename(title="Сохранить результат JSON",
                                            initialfile=f"result_{self.result['work']['work_id']}.json",
                                            defaultextension=".json",
                                            filetypes=[("JSON", "*.json")])
        if name:
            try:
                Path(name).write_text(json.dumps(self.result, ensure_ascii=False, indent=2) + "\n",
                                      encoding="utf-8")
                self.activity.configure(text="JSON сохранён", fg=GREEN)
            except OSError as exc:
                messagebox.showerror("Не удалось сохранить JSON", str(exc))

    def close(self) -> None:
        self.root.destroy()
        self.temp_reports.cleanup()

def main() -> None:
    config = load_config(Path(__file__).with_name("rules.json"))
    root = tk.Tk()
    MonitorApp(root, config)
    root.mainloop()

if __name__ == "__main__":
    main()

