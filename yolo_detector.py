from __future__ import annotations
import math
from collections import Counter
from functools import lru_cache
from pathlib import Path


LABEL_TO_MACHINE = {
    "excavator": "excavator",
    "cran little": "mobile_crane",
    "cran": "tower_crane",
    "samosval": "dump_truck",
    "concrete mixer": "mixer",
    "katok": "roller",
    "svayi": "pile_rig",
    "buldozer": "bulldozer",
}


def _label_key(label: str) -> str:
    return " ".join(str(label).casefold().split())


@lru_cache(maxsize=2)
def _load_model(path: str, modified_ns: int):
    try:
        from ultralytics import YOLO
    except ImportError as exc:
        raise RuntimeError("Не установлен ultralytics. Выполните: python -m pip install ultralytics") from exc
    return YOLO(path)


def detect_counts(image: str | Path, model: str | Path, *, confidence: float = 0.3) -> dict[str, int]:
    """Count all boxes of the eight known dataset classes on one image."""
    image_path = Path(image).expanduser().resolve()
    model_path = Path(model).expanduser().resolve()
    if not image_path.is_file():
        raise FileNotFoundError("Снимок не найден: " + str(image_path))
    if not model_path.is_file():
        raise FileNotFoundError("Веса модели не найдены: " + str(model_path))
    if (not isinstance(confidence, (float, int)) or isinstance(confidence, bool)
            or not math.isfinite(confidence) or not 0 <= confidence <= 1):
        raise ValueError("Порог уверенности YOLO должен быть числом от 0 до 1.")

    try:
        predictions = _load_model(str(model_path), model_path.stat().st_mtime_ns).predict(
            str(image_path), conf=float(confidence), verbose=False)
    except Exception as exc:
        raise RuntimeError("Не удалось выполнить распознавание YOLO: " + str(exc)) from exc
    if len(predictions) != 1:
        raise ValueError("Ожидался один результат YOLO для одного снимка.")
    result = predictions[0]
    if result.boxes is None:
        raise ValueError("Модель не вернула boxes. Нужна модель детекции или сегментации объектов.")

    counts: Counter[str] = Counter()
    for class_id in result.boxes.cls:
        index = int(class_id)
        try:
            label = result.names[index]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError(f"У результата YOLO нет названия класса {index}.") from exc
        machine = LABEL_TO_MACHINE.get(_label_key(label))
        if machine is None:
            raise ValueError(f"Неизвестный класс модели: {label!r}. Добавьте его в LABEL_TO_MACHINE.")
        counts[machine] += 1
    return dict(counts)
