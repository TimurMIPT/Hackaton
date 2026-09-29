from __future__ import annotations
import argparse
import base64
import difflib
import html
import json
import math
import mimetypes
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", str(text).lower().replace("ё", "е")).strip()

def validate_config(config: dict) -> None:
    """Reject broken references and invalid numerical rules before analysis."""
    if config.get("schema_version") != 1:
        raise ValueError("Поддерживается schema_version=1.")
    machines = config["machines"]
    profiles = config["profiles"]
    classes = config["classes"]
    works = config["works"]
    ids = [w["work_id"] for w in works]
    if len(ids) != len(set(ids)):
        raise ValueError("В каталоге повторяются work_id.")
    def valid_number(value, low, high):
        return (isinstance(value, (float, int)) and not isinstance(value, bool)
                and math.isfinite(value) and low <= value <= high)
    for profile in profiles.values():
        if not profile["anchors"] or not set(profile["anchors"]) <= machines.keys():
            raise ValueError("Некорректные опорные типы техники.")
        if not set(profile["weights"]) <= machines.keys():
            raise ValueError("В весах есть неизвестный тип техники.")
        if not all(valid_number(v, 0, 1) for v in profile["weights"].values()):
            raise ValueError("Веса должны быть конечными числами от 0 до 1.")
        if not set(profile["candidate_classes"]) <= classes.keys():
            raise ValueError("Неизвестный класс работ в профиле.")
    for rule in config["rules"].values():
        if not set(rule["profiles"]) <= profiles.keys():
            raise ValueError("Неизвестный профиль в правиле.")
        for group in rule["required"]:
            if not group["any_of"] or not set(group["any_of"]) <= machines.keys():
                raise ValueError("Некорректная группа обязательной техники.")
            value = group["min_count"]
            if type(value) is not int or value < 1:
                raise ValueError("min_count должен быть положительным целым числом.")
    for work in works:
        if work["class_id"] not in {*classes, "G00"} or work["rule_id"] not in config["rules"]:
            raise ValueError("Повреждено сопоставление работы с классом или правилом.")
        if not set(work.get("children", [])) <= set(ids):
            raise ValueError("В группе указана отсутствующая работа.")
        if not set(work.get("group_profiles", []) + work.get("earlier_profiles", [])) <= profiles.keys():
            raise ValueError("Неизвестный профиль в каталоге работ.")
    limits = {"dominant_score":100, "dominant_margin":100, "secondary_score":100,
              "unexpected_share":1, "idle_weight":1}
    for key, high in limits.items():
        if not valid_number(config["thresholds"][key], 0, high):
            raise ValueError("Некорректный порог: " + key)

def load_config(path: str | Path | None = None) -> dict:
    """Load the data layer from --config or rules.json next to the program."""
    if path:
        config_path = Path(path)
    else:
        local_path = Path(__file__).resolve().with_name("rules.json")
        cwd_path = Path.cwd() / "rules.json"
        config_path = local_path if local_path.exists() else cwd_path
    if not config_path.exists():
        raise FileNotFoundError(
            "Не найден rules.json. Положите его рядом с construction_monitor.py "
            "или передайте --config путь/к/rules.json."
        )
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    validate_config(config)
    return config

def aliases(config: dict) -> dict[str, str]:
    result = {}
    for key, machine in config["machines"].items():
        for name in [key, machine["name"], *machine["aliases"]]:
            normalized = normalize(name)
            if normalized in result and result[normalized] != key:
                raise ValueError("Конфликт названий техники: " + name)
            result[normalized] = key
    return result

def canonical_counts(counts: dict, config: dict) -> dict[str, int]:
    names = aliases(config)
    result = {}
    for name, value in counts.items():
        if normalize(name) not in names:
            raise ValueError("Неизвестная техника: " + str(name))
        if type(value) is not int or value < 0:
            raise ValueError("Количество должно быть целым неотрицательным числом: " + str(name))
        key = names[normalize(name)]
        result[key] = result.get(key, 0) + value
    return {key: value for key, value in result.items() if value > 0}

def run_detector(script: str | Path, image: str | Path, config: dict | None = None,
                 *, timeout: int = 120) -> dict[str, int]:
    """Run an external Python detector: ``detect.py image.jpg`` -> JSON on stdout.

    Expected JSON: {"counts": {"excavator": 2, "dump_truck": 1}}.
    The detector may print diagnostics to stderr; stdout must contain only JSON.
    """
    config = config or load_config()
    if type(timeout) is not int or timeout <= 0:
        raise ValueError("Тайм-аут детектора должен быть положительным целым числом секунд.")
    script_path = Path(script).expanduser().resolve()
    image_path = Path(image).expanduser().resolve()
    if not script_path.is_file():
        raise FileNotFoundError("Файл детектора не найден: " + str(script_path))
    if not image_path.is_file():
        raise FileNotFoundError("Снимок не найден: " + str(image_path))
    try:
        process = subprocess.run(
            [sys.executable, str(script_path), str(image_path)],
            cwd=script_path.parent, capture_output=True, text=True,
            encoding="utf-8", timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"Детектор не ответил за {timeout} с.") from exc
    if process.returncode != 0:
        details = process.stderr.strip()[-500:] or "сообщение об ошибке отсутствует"
        raise ValueError(f"Детектор завершился с кодом {process.returncode}: {details}")
    try:
        payload = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("Детектор должен вывести только JSON в stdout: "
                         '{"counts": {"excavator": 2}}.') from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("counts"), dict):
        raise ValueError('Ожидается JSON-объект вида {"counts": {"excavator": 2}}.')
    counts = canonical_counts(payload["counts"], config)
    supported = {key for key, machine in config["machines"].items() if machine["detector"]}
    unavailable = set(counts) - supported
    if unavailable:
        names = ", ".join(config["machines"][key]["name"] for key in sorted(unavailable))
        raise ValueError("Детектор вернул не включённый в правила класс: " + names +
                         ". Отметьте его detector=true в rules.json после проверки модели.")
    return counts

def parse_equipment(text: str, config: dict | None = None) -> dict[str, int]:
    config = config or load_config()
    names = aliases(config)
    if normalize(text) in {"", "нет", "нет техники", "0", "none", "-"}:
        return {}
    counts = {}
    for token in re.split(r"[,;\n]+", text):
        token = normalize(token)
        if not token:
            continue
        count = 1
        name = token
        match = re.fullmatch(r"(.+?)\s*[=:*xх×]\s*(\d+)", token)
        if match:
            name, count = match.group(1).strip(), int(match.group(2))
        elif re.fullmatch(r"\d+\s+.+", token):
            number, name = token.split(" ", 1)
            count = int(number)
        else:
            match = re.fullmatch(r"(.+?)\s+(\d+)", token)
            if match:
                name, count = match.group(1).strip(), int(match.group(2))
        if name not in names:
            close = difflib.get_close_matches(name, names, n=2, cutoff=.6)
            hint = "; возможно: " + ", ".join(close) if close else ""
            raise ValueError(f"Не распознано: {token!r}{hint}. Пример: экскаватор=3. Ввод не принят.")
        key = names[name]
        counts[key] = counts.get(key, 0) + count
    return {key:value for key,value in counts.items() if value > 0}

class WorkSelectionError(ValueError):
    def __init__(self, message, matches=None):
        super().__init__(message)
        self.matches = matches or []

def search_works(query: str, config: dict) -> list[dict]:
    query = normalize(query)
    return [w for w in config["works"] if query in normalize(w["name"]) or query == normalize(w["source_code"])]

def resolve_work(query: str, config: dict | None = None) -> dict:
    config = config or load_config()
    query = normalize(query).rstrip(".")
    row_match = re.fullmatch(r"[rр](\d+)", query)
    if row_match:
        query = f"r{int(row_match.group(1)):03d}"
    matches = [w for w in config["works"] if query in {normalize(w["work_id"]), normalize(w["source_code"])} and query]
    if not matches:
        matches = [w for w in config["works"] if query == normalize(w["name"])]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        matches = search_works(query, config) if query else []
        if len(matches) == 1:
            return matches[0]
    if matches:
        raise WorkSelectionError("Найдено несколько работ. Укажите уникальный ID R… или код.", matches)
    raise WorkSelectionError("Работа не найдена. Используйте --search или --list. Пример кода: 12.3.1.")

def profile_scores(counts: dict, idle: dict, config: dict) -> list[dict]:
    """The selected plan is deliberately absent from this function."""
    iw = config["thresholds"]["idle_weight"]
    effective = {k: n-idle.get(k, 0)+iw*idle.get(k, 0) for k,n in counts.items()}
    mass = {k:math.log2(1+n) for k,n in effective.items()}
    total = sum(mass.values())
    result = []
    for key, profile in config["profiles"].items():
        # A known parked anchor cannot establish an ongoing operation.
        anchors = [k for k in profile["anchors"] if counts.get(k,0)-idle.get(k,0)>0]
        contributions = {k:100*value*profile["weights"].get(k,0)/total for k,value in mass.items()} if anchors and total else {}
        result.append({"profile_id":key,"name":profile["name"],
                       "score":sum(contributions.values()),"anchors":anchors,
                       "contributions":contributions,
                       "candidate_classes":profile["candidate_classes"]})
    return sorted(result, key=lambda x:(-x["score"],x["profile_id"]))


STATUS_NAMES = {
 "compatible":"Техника совместима с выбранной работой",
 "mixed":"Возможное отставание: профили техники не совпадают с планом",
 "plan_unconfirmed":"Возможное отставание: плановый этап не подтверждён",
 "possible_delay":"Возможное отставание: наблюдаемый профиль не совпадает с планом",
 "uncertain":"Возможное отставание: наблюдение не подтверждает план",
 "insufficient":"Возможное отставание: требуемая техника не обнаружена",
 "unobservable":"Возможное отставание: этап не подтверждается доступными признаками",
 "unsupported":"Возможное отставание: текущий детектор не проверяет этот этап",
 "group":"Возможное отставание: выбран общий раздел без подтверждённой подоперации",
 "unlocalized":"Возможное отставание: техника не привязана к плановой операции",
}


def analyze(work: str | dict, counts: dict, config: dict | None = None, *,
            input_mode: str = "manual", scope: str = "whole_site",
            visibility: str = "full", idle: dict | None = None,
            no_haul: bool = False) -> dict:
    """Analyze one snapshot of the whole construction site.

    No work duration, completed quantity, or calendar is supplied; therefore
    confirmed_delay and lag_days are always None. Every non-compatible result
    is surfaced as an actionable possible-delay warning; it is not a confirmed
    schedule violation.
    """
    config = config or load_config()
    if input_mode not in {"manual","detector"} or scope not in {"same_front","whole_site"} or visibility not in {"partial","full"}:
        raise ValueError("Некорректный режим, область наблюдения или видимость.")
    work = resolve_work(work, config) if isinstance(work, str) else work
    counts = canonical_counts(counts, config)
    idle = canonical_counts(idle or {}, config)
    for key, value in idle.items():
        if value > counts.get(key, 0):
            raise ValueError("Простаивающих машин больше, чем найденных: " + config["machines"][key]["name"])
    machines = config["machines"]
    available = set(machines) if input_mode == "manual" else {k for k,v in machines.items() if v["detector"]}
    if not set(counts) <= available:
        raise ValueError("Детектор вернул класс, не включённый в rules.json как detector=true.")
    rule = config["rules"][work["rule_id"]]
    expected = work.get("group_profiles",rule["profiles"]) if work["is_group"] else rule["profiles"]
    required = [] if work["is_group"] else rule["required"]
    ranking = profile_scores(counts, idle, config)
    positive = [p for p in ranking if p["score"]>0]
    top = positive[0] if positive else None
    margin = top["score"]-(positive[1]["score"] if len(positive)>1 else 0) if top else 0
    th = config["thresholds"]
    strong = bool(top and top["score"]>=th["dominant_score"] and margin>=th["dominant_margin"])
    expected_score = max((p["score"] for p in ranking if p["profile_id"] in expected),default=0)
    missing, anomalies, notes = [], [], []
    names = lambda keys: " или ".join(machines[k]["name"] for k in keys)
    if scope == "whole_site":
        notes.append("Принято: снимок охватывает всю строительную площадку. Техника учитывается совместно, включая параллельные работы.")
    else:
        notes.append("Принято: введённая техника относится к одному фронту работ внутри площадки.")
    if visibility == "partial":
        notes.append("Обзор частичный: ненайденная техника может находиться вне кадра. Предупреждение о ненаблюдении не означает доказанного отсутствия.")
    else:
        notes.append("Обзор полной площадки: отсутствие обязательной техники трактуется как сигнал для проверки возможного отставания.")
    notes.append("Количество машин не показывает производительность, загрузку или выполненный объём.")
    if ("pile_rig" in counts and input_mode == "manual"
            and not machines["pile_rig"]["detector"]):
        notes.append("Свайная установка введена вручную; текущий детектор её не распознаёт.")
    if idle:
        notes.append("Простой задан пользователем отдельно; программа не выводит его из малого количества машин.")
    for group in required:
        if no_haul and group.get("haul_only"):
            continue
        options = group["any_of"]
        # OR means any single alternative must reach the threshold, not their sum.
        observed = max(counts.get(k,0) for k in options)
        if observed < group["min_count"]:
            if not set(options) <= available:
                anomalies.append({"type":"unsupported_requirement","message":f"Нельзя проверить наличие: {names(options)} — не все альтернативы распознаются.","severity":"info"})
            else:
                item={"any_of":options,"min_count":group["min_count"],"observed":observed}
                missing.append(item)
                if visibility == "partial":
                    detail = " в зоне обзора"
                elif scope == "whole_site":
                    detail = " на снимке всей площадки"
                else:
                    detail = " в указанной зоне"
                prefix = "Возможное отставание: " if visibility == "full" else "Сигнал к проверке: "
                anomalies.append({"type":"not_observed","message":f"{prefix}на этапе «{work['name']}» не наблюдается {names(options)}{detail}; правило ожидает минимум {group['min_count']}. {group.get('note','')}","severity":"warning"})
        elif all(counts.get(k,0)-idle.get(k,0)==0 for k in options):
            anomalies.append({"type":"idle_required","message":f"{names(options)} присутствует, но вся эта техника отмечена как простаивающая.","severity":"warning"})
    supported_expected = [p for p in expected if set(config["profiles"][p]["anchors"]) & available]
    allowed = {k for p in expected for k,v in config["profiles"][p]["weights"].items() if v>0}
    effective = {k:math.log2(1+n-idle.get(k,0)+th["idle_weight"]*idle.get(k,0)) for k,n in counts.items()}
    total = sum(effective.values())
    foreign = {k:n for k,n in counts.items() if k not in allowed}
    foreign_share = sum(effective[k] for k in foreign)/total if total else 0
    if expected and foreign:
        level = "warning" if foreign_share >= th["unexpected_share"] and scope=="same_front" else "info"
        anomalies.append({"type":"uncharacteristic_equipment","equipment":foreign,"share":foreign_share,
                          "severity":level,"message":"Нехарактерная для выбранного правила техника: "+", ".join(f"{machines[k]['name']} × {v}" for k,v in foreign.items())+". Возможны соседние работы, доставка или ожидание."})
    secondary = [p for p in positive[1:] if p["score"]>=th["secondary_score"]]
    if secondary:
        anomalies.append({"type":"multiple_profiles","severity":"info","message":"Есть дополнительные признаки: "+", ".join(p["name"] for p in secondary)+". Они не доказывают простой или параллельное выполнение."})
    earlier = bool(top and top["profile_id"] in work.get("earlier_profiles",[]))
    reasons = []
    if not expected:
        status="unobservable"
        reasons.append("Возможное отставание: текущий набор детектора не даёт достаточного визуального признака для этой операции. Проверьте календарный план и повторите наблюдение.")
    elif not supported_expected:
        status="unsupported"
        reasons.append("Возможное отставание: обязательный профиль этого этапа не входит в текущий набор классов детектора. Нужен дополнительный класс распознавания или ручная проверка.")
    elif work["is_group"]:
        status="group"
        reasons.append("Возможное отставание: выбран общий раздел, а состав техники не подтверждает конкретную подоперацию. Откройте строку работы ниже по разделу.")
    elif not positive:
        status="insufficient"
        reasons.append("Возможное отставание: на полном снимке площадки не обнаружены опорные типы техники для выбранной операции. Проверьте простой, ручные работы и календарный план.")
    elif strong and top["profile_id"] not in expected:
        if expected_score>=th["secondary_score"]:
            status="mixed"
            reasons.append("Возможное отставание: плановый профиль присутствует, но количественно преобладает другой профиль. Проверьте параллельные работы и фактический фронт.")
        elif earlier and visibility=="full":
            status="possible_delay"
            reasons.append("Возможное отставание: преобладает профиль, который относится к более ранней операции, тогда как план указывает на конструктивные работы.")
            reasons.append("Проверьте календарный план и причины: обратная засыпка, смежные работы и ожидание техники также возможны.")
        else:
            status="plan_unconfirmed"
            reasons.append("Возможное отставание: преобладает другой наблюдаемый профиль, поэтому плановый этап не подтверждён.")
            if earlier:
                reasons.append("Проверьте, не выполняется ли более ранняя операция или параллельный этап.")
    elif missing or any(a["type"]=="idle_required" for a in anomalies):
        status="plan_unconfirmed"
        reasons.append("Возможное отставание: набор техники не выполняет все условия выбранного сценария. Проверьте обязательную технику и календарный план.")
    elif strong and top["profile_id"] in expected:
        status="compatible"
        reasons.append("Преобладающий профиль согласуется с выбранной работой. Это подтверждает совместимость техники, но не соблюдение сроков.")
    elif expected_score>0 and secondary:
        status="mixed"
        reasons.append("Возможное отставание: несколько профилей имеют заметную поддержку, и плановый этап не является однозначно ведущим.")
    else:
        status="uncertain"
        reasons.append("Возможное отставание: ведущий профиль недостаточно выражен для подтверждения планового этапа. Проверьте календарь и повторите наблюдение.")
    if top:
        notes.append("Один профиль соответствует нескольким этапам. План не используется для изменения рейтинга видимой техники.")
    lag = "not_established" if status=="compatible" else "possible"
    return {"version":1,"timestamp":datetime.now(timezone.utc).isoformat(),
            "work":{k:work[k] for k in ("work_id","source_row","source_code","name","path","class_id","rule_id","is_group")},
            "class_name":config["classes"].get(work["class_id"],{"name":"Сводный раздел"})["name"],
            "counts":counts,"idle_counts":idle,"input_mode":input_mode,"scope":scope,"visibility":visibility,
            "status":status,"status_text":STATUS_NAMES[status],"lag_status":lag,
            "confirmed_delay":None,"lag_days":None,"expected_profiles":expected,
            "expected_score":expected_score,"ranking":ranking,"dominant_profile":top["profile_id"] if strong else None,
            "leader_profile":top["profile_id"] if top else None,"margin":margin,
            "profile_evidence":"Выраженное преобладание" if strong else "Неоднозначный состав",
            "missing":missing,"anomalies":anomalies,"reasons":reasons,"notes":notes,
            "rule_note":rule["note"],"mapping_note":work["mapping_note"],
            "score_kind":"Экспертные баллы 0–100, не вероятность и не измеренная точность"}

def print_work_choices(works, config, limit=None):
    visible = works[:limit] if limit else works
    for w in visible:
        label = " [раздел]" if w["is_group"] else ""
        print(f"{w['work_id']} | {w['source_code'] or 'без кода'} | {w['name']}{label}")
        print("    " + w["path"])
    if limit and len(works)>limit:
        print(f"Показано {limit} из {len(works)}. Уточните запрос.")

def _without_warning_prefix(text: str) -> str:
    return re.sub(r"^Возможное отставание:\s*", "", text).strip()

def _compact_cause(result: dict) -> str:
    """Choose one human-readable cause instead of printing every diagnostic."""
    if result["status"] == "group":
        return "выбран общий раздел; конкретная подоперация не подтверждена."
    not_observed = next((a for a in result["anomalies"] if a["type"] == "not_observed"), None)
    if not_observed:
        return _without_warning_prefix(not_observed["message"])
    if result["reasons"]:
        return _without_warning_prefix(result["reasons"][0])
    return "Недостаточно данных для объяснения результата."

def _compact_action(result: dict, config: dict) -> str:
    status = result["status"]
    if status == "compatible":
        return "Продолжить наблюдение: совместимость техники не подтверждает сроки и выполненный объём."
    if status == "group":
        code = result["work"]["source_code"] or result["work"]["work_id"]
        return f"Открыть конкретную подоперацию внутри раздела {code} и проверить её календарный план."
    if result["missing"]:
        names = []
        for item in result["missing"]:
            names.append(" или ".join(config["machines"][k]["name"] for k in item["any_of"]))
        action = "Проверить наличие: " + "; ".join(names) + "."
        if any(a.get("type") == "not_observed" and "вывоз" in a.get("message", "") for a in result["anomalies"]):
            action += " Если грунт не вывозится, повторить расчёт с --no-haul."
        return action
    actions = {
        "mixed": "Проверить параллельные работы и фактический фронт.",
        "possible_delay": "Проверить календарный план и фактический фронт.",
        "unsupported": "Проверить этап вручную или добавить недостающий класс распознавания.",
        "unobservable": "Проверить этап вручную и повторить наблюдение.",
        "insufficient": "Проверить простой техники и повторить снимок.",
        "uncertain": "Повторить снимок и уточнить выбранную операцию.",
        "unlocalized": "Уточнить зону работ и повторить наблюдение.",
    }
    return actions.get(status, "Проверить календарный план и причину отклонения.")

def _compact_profile(result: dict) -> str:
    top = next((p for p in result["ranking"] if p["score"] > 0), None)
    if not top:
        return "Признак: опорная техника не обнаружена."
    secondary = [p["name"] for p in result["ranking"][1:] if p["score"] >= 20]
    text = f"Признак: ведущий профиль — «{top['name']}»"
    if secondary:
        text += "; дополнительные признаки — " + ", ".join(secondary)
    return text + "."

def summarize_result(result: dict, config: dict) -> dict[str, str]:
    """Human-readable decision shared by the terminal and desktop app."""
    return {
        "decision": "СОВМЕСТИМО С ПЛАНОМ" if result["status"] == "compatible"
                    else "ВОЗМОЖНОЕ ОТСТАВАНИЕ",
        "cause": _compact_cause(result),
        "profile": _compact_profile(result),
        "action": _compact_action(result, config),
    }

def print_result(result: dict, config: dict, *, verbose: bool = False):
    """Print a short operational answer; use --verbose for diagnostics."""
    w = result["work"]
    equipment = ", ".join(
        f"{config['machines'][k]['name']}={n}" for k, n in result["counts"].items()
    ) or "техника не найдена"
    if not verbose:
        summary = summarize_result(result, config)
        print(f"\nПлан: {w['work_id']} / {w['source_code'] or 'без кода'} — {w['name']}")
        print("Наблюдение: " + equipment)
        print("\nРЕШЕНИЕ: " + summary["decision"])
        print("Причина: " + summary["cause"])
        print(summary["profile"])
        print("Действие: " + summary["action"])
        return

    # The detailed view is intentionally opt-in: it is useful for debugging and
    # method review, but is too verbose for an operator's normal terminal run.
    print(f"\nПлан: {w['work_id']} / {w['source_code'] or 'без кода'} — {w['name']}")
    print(f"Класс: {w['class_id']} — {result['class_name']}")
    print("Наблюдение: " + equipment)
    print("\nРЕЗУЛЬТАТ: " + result["status_text"])
    lag_labels={"possible":"ВОЗМОЖНОЕ ОТСТАВАНИЕ — требуется проверить календарный план и причину", "not_established":"признак отставания не найден", "not_assessable":"проверка не выполнялась"}
    print("Отставание: " + lag_labels[result["lag_status"]])
    print("\nПрофили техники (баллы 0–100, не вероятности):")
    for item in result["ranking"]:
        print(f"  {item['profile_id']}  {item['name']}: {item['score']:.1f}")
    print(f"Отрыв первых двух профилей: {result['margin']:.1f} балла. {result['profile_evidence']}.")
    top = next((p for p in result["ranking"] if p["score"]>0),None)
    if top:
        print("Классы, совместимые с ведущим профилем: " + ", ".join(f"{k} ({config['classes'][k]['name']})" for k in top["candidate_classes"]))
    for reason in result["reasons"]:
        print("\n"+reason)
    if result["anomalies"]:
        print("\nПредупреждения и наблюдения:")
        for item in result["anomalies"]:
            print("  • " + item["message"])
    print("\nСценарий правила: " + result["rule_note"])
    if result["mapping_note"]:
        print("Привязка строки: " + result["mapping_note"])
    print("\nУсловия вывода:")
    for note in result["notes"]:
        print("  • " + note)

def write_html(result: dict, path: str | Path, config: dict, *, embed_image: bool = False) -> None:
    """Write an offline report; optionally embed the image for a portable HTML file."""
    esc=lambda value:html.escape(str(value),quote=True)
    color="#16705b" if result["status"]=="compatible" else "#a36310"
    summary=summarize_result(result,config)
    source_label=("Снимок обработан YOLO" if result.get("detector_backend")=="yolo" else
                  "Снимок обработан внешним детектором" if result.get("source_image") else
                  "Демонстрационный расчёт")
    model_note=(f"Модель: {esc(Path(result['model_path']).name)} · порог {result['confidence_threshold']:.2f}"
                if result.get("detector_backend")=="yolo" else "")
    snapshot=""
    if result.get("source_image"):
        source=Path(result["source_image"]).resolve()
        if embed_image:
            mime=mimetypes.guess_type(source.name)[0]
            if not mime or not mime.startswith("image/"):
                raise ValueError("Отчёт может встроить только файл изображения.")
            encoded=base64.b64encode(source.read_bytes()).decode("ascii")
            image_ref=f"data:{mime};base64,{encoded}"
        else:
            try:
                image_ref=quote(Path(os.path.relpath(source,Path(path).resolve().parent)).as_posix(),safe="/.-_")
            except ValueError:  # Different drives on Windows.
                image_ref=source.as_uri()
        snapshot=f'<section class="snapshot"><h2>Снимок площадки</h2><img src="{esc(image_ref)}" alt="Исходный снимок площадки"><p class="muted">{esc(source.name)}</p></section>'
    equipment="".join(f"<tr><td>{esc(config['machines'][key]['name'])}</td><td>{count}</td><td>{result['idle_counts'].get(key,0)}</td></tr>" for key,count in result["counts"].items()) or '<tr><td colspan="3">Техника не найдена</td></tr>'
    scores="".join(f"<div class='profile'><div><b>{esc(p['name'])}</b><span>{p['score']:.1f}</span></div><div class='track'><div style='width:{p['score']:.4f}%'></div></div></div>" for p in result["ranking"])
    li=lambda values:"".join(f"<li>{esc(v)}</li>" for v in values)
    alerts=li([a["message"] for a in result["anomalies"]]) or '<li>Дополнительных предупреждений нет.</li>'
    page=f'''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Мониторинг строительных работ</title>
<style>body{{font:16px/1.5 Arial,sans-serif;color:#1c2c3d;background:#eef2f5;margin:0}}main{{max-width:1100px;margin:32px auto;padding:30px;background:white;border-radius:16px}}h1{{font-size:28px;margin:0 0 8px}}h2{{font-size:20px}}.muted{{color:#536577}}.status{{padding:18px;border-left:5px solid {color};background:#f8f3e8;margin:24px 0}}.grid{{display:grid;grid-template-columns:1fr 1fr;gap:32px}}table{{border-collapse:collapse;width:100%}}th,td{{text-align:left;border-bottom:1px solid #dce4ec;padding:10px}}th{{background:#e9eff5}}.profile{{margin:16px 0}}.profile>div:first-child{{display:flex;justify-content:space-between;gap:12px}}.track{{height:9px;background:#e8eef3;margin-top:7px;border-radius:8px;overflow:hidden}}.track>div{{height:100%;background:#3d648d}}.snapshot img{{max-width:100%;max-height:560px;border-radius:8px}}li{{margin:8px 0}}@media(max-width:700px){{main{{margin:0;padding:20px}}.grid{{grid-template-columns:1fr}}}}</style>
<main><div class="muted">Прототип · {source_label}</div><h1>Сопоставление наблюдения с планом</h1>
<p><b>{esc(result['work']['name'])}</b><br>{esc(result['work']['work_id'])} · {esc(result['class_name'])}<br>{model_note}</p>
<div class="status"><div class="muted">РЕШЕНИЕ</div><h2>{esc(summary['decision'])}</h2>
<p><b>Причина:</b> {esc(summary['cause'])}</p><p>{esc(summary['profile'])}</p>
<p><b>Действие:</b> {esc(summary['action'])}</p></div>
{snapshot}
<div class="grid"><section><h2>Наблюдаемая техника</h2><table><thead><tr><th>Тип</th><th>Всего</th><th>Заданный простой</th></tr></thead><tbody>{equipment}</tbody></table></section>
<section><h2>Профили по количеству</h2><p class="muted">Баллы 0–100, не вероятности. План не влияет на рейтинг.</p>{scores}</section></div>
<h2>Объяснение</h2><ul>{li(result['reasons'])}</ul><h2>Предупреждения</h2><ul>{alerts}</ul>
<h2>Условия</h2><p>{esc(result['rule_note'])}</p><ul>{li(result['notes'])}</ul>
<p class="muted">Это сигнал для проверки. По одному снимку нельзя подтвердить отставание в днях.</p>
<p class="muted">Создано: {esc(result['timestamp'])}</p></main></html>'''
    Path(path).write_text(page,encoding="utf-8")

def main(argv=None) -> int:
    parser=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--work",help="Код работы, уникальный ID R047 или название")
    parser.add_argument("--image",help="Путь к снимку всей строительной площадки")
    source=parser.add_mutually_exclusive_group()
    source.add_argument("--model",help="Веса YOLO best.pt (метки модели переводятся автоматически)")
    source.add_argument("--detector-script",help="Альтернативный Python-детектор: получает снимок, выводит JSON")
    parser.add_argument("--confidence",type=float,default=0.3,metavar="0..1",
                        help="Порог уверенности YOLO (по умолчанию 0.3)")
    parser.add_argument("--detector-timeout",type=int,default=120,metavar="SECONDS",
                        help="Максимальное время работы детектора (по умолчанию 120 с)")
    parser.add_argument("--idle",default="",help="Из общего количества отдельно отмеченные простаивающие машины")
    parser.add_argument("--scope",choices=["same_front","whole_site"],default="whole_site")
    parser.add_argument("--visibility",choices=["partial","full"],default="full")
    parser.add_argument("--no-haul",action="store_true",help="Не требовать самосвалы в сценарии без вывоза")
    parser.add_argument("--config",help="Внешний rules.json вместо встроенных правил")
    parser.add_argument("--export-config",metavar="PATH",help="Сохранить текущие правила для редактирования")
    parser.add_argument("--list",action="store_true",help="Показать весь каталог")
    parser.add_argument("--search",help="Поиск по названию работы")
    parser.add_argument("--json-out",metavar="PATH",help="Сохранить результат в JSON")
    parser.add_argument("--html",metavar="PATH",help="Сохранить локальный наглядный HTML-отчёт")
    parser.add_argument("--verbose",action="store_true",help="Показать подробные баллы, правила и ограничения")
    args=parser.parse_args(argv)
    try:
        config=load_config(args.config)
        if args.export_config:
            Path(args.export_config).write_text(json.dumps(config,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
            print("Правила сохранены: " + args.export_config)
            return 0
        if args.list or args.search is not None:
            choices=search_works(args.search,config) if args.search is not None else config["works"]
            print_work_choices(choices,config)
            print(f"Найдено: {len(choices)}")
            return 0
        if not args.image or not (args.model or args.detector_script):
            raise ValueError("Для анализа укажите --image и --model (или --detector-script). "
                             "Список работ доступен через --search или --list.")
        work=None
        if args.work is not None:
            work=resolve_work(args.work,config)
        while work is None:
            value=input("\nПлановая работа: ").strip()
            if normalize(value) in {"выход","exit","quit"}:
                return 0
            if normalize(value)=="список":
                print_work_choices(config["works"],config)
                continue
            if normalize(value).startswith("поиск "):
                print_work_choices(search_works(value[6:],config),config,30)
                continue
            try:
                work=resolve_work(value,config)
            except WorkSelectionError as exc:
                print(str(exc)); print_work_choices(exc.matches,config,20)
        if args.model:
            from yolo_detector import detect_counts
            counts=detect_counts(args.image,args.model,confidence=args.confidence)
        else:
            counts=run_detector(args.detector_script,args.image,config,timeout=args.detector_timeout)
        result=analyze(work,counts,config,input_mode="detector",scope=args.scope,
                       visibility=args.visibility,idle=parse_equipment(args.idle,config),no_haul=args.no_haul)
        result["source_image"]=str(Path(args.image).expanduser().resolve())
        result["detector_backend"]="yolo" if args.model else "external_script"
        if args.model:
            result["model_path"]=str(Path(args.model).expanduser().resolve())
            result["confidence_threshold"]=args.confidence
        print_result(result,config,verbose=args.verbose)
        if args.json_out:
            Path(args.json_out).write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
            print("\nJSON сохранён: " + args.json_out)
        if args.html:
            write_html(result,args.html,config)
            print("HTML-отчёт сохранён: " + args.html)
        return 0
    except WorkSelectionError as exc:
        print("Ошибка: "+str(exc),file=sys.stderr)
        print_work_choices(exc.matches,locals().get("config",{}),20)
        return 2
    except (ValueError,KeyError,TypeError,OSError,RuntimeError) as exc:
        print("Ошибка: "+str(exc),file=sys.stderr)
        return 2
    except (EOFError,KeyboardInterrupt):
        print("\nВвод завершён.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
