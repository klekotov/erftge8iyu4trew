#!/usr/bin/env python3
"""
Инструмент для работы с JSON-файлами лотов, которые принимает плагин Lots Copy (/create_lots).

Команды:

1) Шаблон под конкретный раздел FunPay (какие поля обязательны и какие значения допустимы):

       python tools/lot_json_tool.py template 1089
       python tools/lot_json_tool.py template 1089 --out template_1089.json

   Скрипт делает GET https://funpay.com/lots/offerEdit?node=<node_id> тем же аккаунтом,
   что и Cardinal (golden_key берётся из configs/_main.cfg), достаёт из формы ВСЕ поля,
   а также подписи полей, признак "обязательное" и список допустимых значений для select'ов.

2) Проверка готового JSON перед загрузкой в /create_lots (без обращения к FunPay):

       python tools/lot_json_tool.py validate lots.json
       python tools/lot_json_tool.py validate lots.json --template template_1089.json

   Проверяются ровно те правила, которые применяет FunPayAPI при сохранении лота
   (FunPayAPI/types.py -> LotFields.__init__ / renew_fields), поэтому все ошибки,
   которые обычно вылезают как "❌ Не удалось создать лот ..." уже на FunPay,
   ловятся локально.

3) Проставить ID раздела всем лотам файла (например, после подготовки текстов):

       python tools/lot_json_tool.py set-node lots.json 1666

4) Добавить поле во все лоты сразу (например, выбор игры в разделе "Прочие игры"):

       python tools/lot_json_tool.py add-field lots.json "fields[game]" "Car Dealership Tycoon" --node 402

   С --node значение-подпись автоматически превращается в значение опции формы (часто число),
   а несуществующее значение даёт ошибку со списком допустимых.

5) Генерация большого количества лотов из CSV (Excel): одна строка = один лот.

       python tools/lot_json_tool.py gen --template template_1089.json --rows lots.csv --out lots.json

   Колонки CSV (заголовки в первой строке, разделитель можно указать через --delimiter):
       summary_ru        (обязательно) краткое описание RU
       desc_ru           (обязательно) полное описание RU
       price             (обязательно) цена за 1 шт, например 199 или 199.50
       amount                          наличие, целое число (если в разделе есть наличие)
       summary_en, desc_en, payment_msg_ru, payment_msg_en
       secrets                         товары встроенной автовыдачи, по одному в строке, разделитель |
       node_id, active, auto_delivery, deactivate_after_sale
   В текстовых колонках последовательность \\n превращается в перенос строки.

Требования: те же, что и у Cardinal (requests, beautifulsoup4, lxml, requests_toolbelt).
Запускать из корня Cardinal, чтобы нашлась папка FunPayAPI.
"""

from __future__ import annotations

import argparse
import configparser
import csv
import json
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_CONFIG = REPO_ROOT / "configs" / "_main.cfg"

# Поля, которые плагин / FunPayAPI формируют сами либо которые должны оставаться пустыми.
SERVICE_FIELDS = {"csrf_token", "offer_id", "query", "location", "deleted"}

# Порядок ключей в шаблоне (для читаемого вывода).
KEY_ORDER = [
    "node_id",
    "active",
    "price",
    "amount",
    "deactivate_after_sale",
    "fields[summary][ru]",
    "fields[summary][en]",
    "fields[desc][ru]",
    "fields[desc][en]",
    "fields[payment_msg][ru]",
    "fields[payment_msg][en]",
    "fields[images]",
    "auto_delivery",
    "secrets",
    "form_created_at",
]

CSV_COLUMNS = [
    "summary_ru", "summary_en",
    "desc_ru", "desc_en",
    "payment_msg_ru", "payment_msg_en",
    "price", "amount",
    "secrets",
    "node_id", "active", "auto_delivery", "deactivate_after_sale",
]

# Какая колонка CSV соответствует какому полю лота.
CSV_TO_FIELD = {
    "summary_ru": "fields[summary][ru]",
    "summary_en": "fields[summary][en]",
    "desc_ru": "fields[desc][ru]",
    "desc_en": "fields[desc][en]",
    "payment_msg_ru": "fields[payment_msg][ru]",
    "payment_msg_en": "fields[payment_msg][en]",
    "price": "price",
    "amount": "amount",
    "secrets": "secrets",
    "node_id": "node_id",
    "active": "active",
    "auto_delivery": "auto_delivery",
    "deactivate_after_sale": "deactivate_after_sale",
}


# --------------------------------------------------------------------------------------
# Общие утилиты
# --------------------------------------------------------------------------------------

def die(message: str, code: int = 1):
    print(f"❌ {message}", file=sys.stderr)
    raise SystemExit(code)


def ordered(fields: dict[str, Any]) -> dict[str, Any]:
    """Возвращает словарь с ключами в удобном для чтения порядке."""
    result = {}
    for key in KEY_ORDER:
        if key in fields:
            result[key] = fields[key]
    for key in fields:
        if key not in result:
            result[key] = fields[key]
    return result


def load_json(path: str | Path) -> Any:
    p = Path(path)
    if not p.exists():
        die(f"Файл не найден: {p}")
    try:
        with p.open("r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        die(f"{p}: некорректный JSON ({e.msg}, строка {e.lineno}, колонка {e.colno}). "
            f"Частая причина - лишняя запятая или незакрытая кавычка.")


def write_json(path: str | Path, data: Any):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)
    print(f"💾 Сохранено: {path}")


def load_account(config_path: str | Path = DEFAULT_CONFIG):
    """Создаёт и инициализирует FunPayAPI.account.Account по golden_key из конфига Cardinal."""
    from FunPayAPI.account import Account

    cfg_path = Path(config_path)
    if not cfg_path.exists():
        die(f"Не найден конфиг {cfg_path} (в нём хранится golden_key).")
    cfg = configparser.ConfigParser()
    cfg.read(cfg_path, encoding="utf-8")
    if not cfg.has_section("FunPay"):
        die(f"В {cfg_path} нет секции [FunPay].")
    token = (cfg.get("FunPay", "golden_key", fallback="") or "").strip()
    if len(token) != 32:
        die("В конфиге пустой / неверный golden_key.")
    user_agent = (cfg.get("FunPay", "user_agent", fallback="") or "").strip() or None
    try:
        return Account(token, user_agent).get()
    except Exception as e:  # сеть / протухший токен
        die(f"Не удалось авторизоваться на FunPay: {type(e).__name__}: {e}")


# --------------------------------------------------------------------------------------
# команда template
# --------------------------------------------------------------------------------------

def parse_offer_edit_form(html: str) -> tuple[dict[str, str], dict[str, dict]]:
    """
    Разбирает форму /lots/offerEdit так же, как FunPayAPI.account.get_lot_fields,
    но дополнительно собирает метаданные: подпись поля, обязательность, варианты select.

    :return: (поля, метаданные)
    """
    from bs4 import BeautifulSoup

    bs = BeautifulSoup(html, "lxml")
    error = bs.find("p", class_="lead")
    if error:
        die(f"FunPay не отдал форму лота: {error.text.strip()}")

    form = bs.find("form", class_="form-offer-editor")
    if not form:
        die("На странице нет формы form-offer-editor. Скорее всего node_id неверный "
            "или у аккаунта нет доступа к разделу (часто требуется верификация продавца).")

    fields: dict[str, str] = {}
    for inp in form.find_all("input"):
        name = inp.get("name")
        if name:
            fields[name] = inp.get("value") or ""
    for ta in form.find_all("textarea"):
        if ta.get("name"):
            fields[ta["name"]] = ta.text or ""
    for sel in form.find_all("select"):
        parent = sel.find_parent(class_="form-group")
        if parent and "hidden" in parent.get("class", []):
            continue
        if not sel.get("name"):
            continue
        option = sel.find("option", selected=True) or sel.find("option")
        fields[sel["name"]] = option.get("value", "") if option else ""
    for cb in form.find_all("input", {"type": "checkbox"}, checked=True):
        if cb.get("name"):
            fields[cb["name"]] = "on"

    meta: dict[str, dict] = {}
    for group in form.find_all("div", class_="form-group"):
        label = group.find("label")
        control = group.find(["input", "select", "textarea"])
        if not label or not control or not control.get("name"):
            continue
        name = control["name"]
        if not name.startswith("fields[") or name == "fields[images]":
            continue
        options = []
        if control.name == "select":
            options = [(o.get("value", ""), o.get_text(strip=True))
                       for o in control.find_all("option") if o.get("value")]
        meta[name] = {
            "label": label.get_text(strip=True).rstrip("*").strip(),
            "required": ("required" in group.get("class", [])
                         or bool(control.get("required"))
                         or bool(label.find("span", class_="text-danger"))),
            "options": options,
        }
    return fields, meta


def cmd_template(args):
    acc = load_account(args.config)
    print(f"🔎 Запрашиваю форму раздела node_id={args.node_id}...")
    response = acc.method("get", f"lots/offerEdit?node={args.node_id}", {}, {}, raise_not_200=True)
    html = response.content.decode()
    if "Доступ запрещен" in html:
        die(f"FunPay вернул 'Доступ запрещен' для раздела {args.node_id}. "
            f"Обычно это значит, что раздел требует верификации продавца.")

    fields, meta = parse_offer_edit_form(html)

    # Плагин сам подставляет csrf_token текущего аккаунта и offer_id = 0,
    # поэтому в шаблоне им делать нечего.
    fields.pop("csrf_token", None)
    fields.pop("offer_id", None)

    template_lot = ordered(fields)

    if meta:
        print("\n📋 Поля раздела (кроме стандартных summary/desc/payment_msg/images):")
        print(f"{'поле':<28} {'обяз.':<6} описание / допустимые значения")
        print("-" * 100)
        for name, info in meta.items():
            options = ", ".join(f"{v} = {label}" for v, label in info["options"]) or "любая строка"
            print(f"{name:<28} {'да' if info['required'] else 'нет':<6} {info['label']}")
            print(f"{'':<28} {'':<6}   значения: {options}")
        print()

    print(json.dumps([template_lot], indent=4, ensure_ascii=False))

    out = args.out or f"template_{args.node_id}.json"
    write_json(out, [template_lot])
    meta_path = str(Path(out).with_suffix("")) + ".meta.json"
    write_json(meta_path, {"node_id": str(args.node_id), "fields": meta})
    print("\n👉 Дальше: отредактируйте price/description/… и отправьте файл боту командой /create_lots.")


# --------------------------------------------------------------------------------------
# команда validate
# --------------------------------------------------------------------------------------

def simulate_funpayapi(lot: dict) -> str | None:
    """
    Прогоняет лот через настоящий LotFields.renew_fields() FunPayAPI.

    :return: текст ошибки или None, если ошибки нет.
    """
    try:
        from FunPayAPI import types
    except Exception:
        return None  # FunPayAPI недоступен - работаем только по статическим правилам
    try:
        lot_obj = types.LotFields(0, dict(lot))
        lot_obj.csrf_token = "TEST"
        lot_obj.lot_id = 0
        lot_obj.renew_fields().fields
    except Exception as e:
        return f"{type(e).__name__}: {e}"
    return None


def validate_lot(index: int, lot: Any, meta: dict | None) -> list[tuple[str, str]]:
    """
    Проверяет один лот.

    :return: список (уровень, сообщение): уровень "ERR" или "WARN".
    """
    issues: list[tuple[str, str]] = []
    if not isinstance(lot, dict):
        return [("ERR", f"элемент #{index} не объект JSON")]

    for key, value in lot.items():
        if not isinstance(value, str):
            issues.append(("WARN", f"{key}: значение не строка ({type(value).__name__}). "
                                   f"Плагин обычно переварит число, но лучше всегда писать строки."))

    node_id = lot.get("node_id")
    if node_id is None:
        issues.append(("ERR", "нет node_id - FunPay не поймёт, в какой раздел создавать лот"))
    elif not (isinstance(node_id, (str, int)) and str(node_id).isdigit()):
        issues.append(("ERR", f"node_id={node_id!r} должен быть числом (ID раздела из ссылки "
                              f"https://funpay.com/lots/<node_id>/trade)"))

    # Служебные ключи формы: они есть в каждой выгрузке /cache_lots (пустыми),
    # и FunPay отвечает "Заполните все поля.", если их нет в запросе.
    for service_key in ("location", "deleted"):
        if service_key not in lot:
            issues.append(("WARN", f"нет {service_key}: в выгрузке /cache_lots это поле есть пустым. "
                                   f"Добавьте \"{service_key}\": \"\", иначе FunPay может ответить "
                                   f"\"Заполните все поля.\""))

    price = lot.get("price")
    if price in (None, ""):
        issues.append(("ERR", "нет price - лот не сохранится"))
    else:
        try:
            price_value = float(price)
            if price_value <= 0:
                issues.append(("ERR", f"price={price!r} должен быть > 0"))
        except (TypeError, ValueError):
            issues.append(("ERR", f"price={price!r} не число. Разделитель - точка, без пробелов: "
                                  f"199 или 199.50 (1,5 / 1 000 - не годится)"))

    amount = lot.get("amount")
    if amount not in (None, ""):
        if not (isinstance(amount, int) or (isinstance(amount, str) and amount.isdigit())):
            issues.append(("ERR", f"amount={amount!r} должен быть целым числом без точки: "
                                  f"9999, а не 9999.0 / 9 999"))

    active = lot.get("active")
    if "active" not in lot:
        issues.append(("WARN", "нет active: лот создастся выключенным (для публикации нужно \"on\")"))
    elif active not in (None, "", "on"):
        issues.append(("ERR", f"active={active!r}: плагин считает лот активным только при ровно "
                              f"\"on\". Любое другое значение = лот создаётся выключенным."))

    for flag in ("auto_delivery", "deactivate_after_sale"):
        if flag in lot and lot[flag] not in ("", "on"):
            issues.append(("WARN", f"{flag}={lot[flag]!r}: любая непустая строка превращается "
                                   f"в \"on\" (флаг включится). Чтобы выключить - только \"\"."))

    secrets = lot.get("secrets")
    if secrets is not None and not isinstance(secrets, str):
        issues.append(("ERR", "secrets должен быть строкой (товары через \\n), а не списком"))
    elif isinstance(secrets, str) and secrets.strip():
        if lot.get("auto_delivery") != "on":
            issues.append(("WARN", "secrets заполнены, но auto_delivery != \"on\": товары сохранятся, "
                                   "а встроенная автовыдача останется выключенной"))

    images = lot.get("fields[images]")
    if images not in (None, ""):
        parts = [p for p in str(images).split(",") if p]
        if any(not p.strip().isdigit() for p in parts):
            issues.append(("ERR", f"fields[images]={images!r} должен быть списком числовых ID "
                                  f"через запятую: 123456,789012"))

    if "deleted" in lot and lot["deleted"] not in ("", None):
        issues.append(("ERR", "deleted не пустой: FunPay попытается удалить лот, а не создать"))

    if "csrf_token" in lot:
        issues.append(("WARN", "csrf_token в файле не нужен: плагин всё равно подставит токен аккаунта"))
    if "offer_id" in lot and str(lot["offer_id"]) not in ("0", ""):
        issues.append(("WARN", f"offer_id={lot['offer_id']}: плагин принудительно ставит \"0\", "
                               f"т.е. всегда создаёт новый лот"))

    for name in ("fields[summary][ru]", "fields[desc][ru]"):
        if not str(lot.get(name) or "").strip():
            issues.append(("WARN", f"{name} пустое - лот будет с пустым названием/описанием"))

    if meta:
        for name, info in meta.items():
            if info.get("required") and not str(lot.get(name) or "").strip():
                issues.append(("ERR", f"{name} ({info.get('label')}) обязательно в этом разделе"))
            if info.get("options") and name in lot:
                allowed = {value for value, _ in info["options"]}
                if str(lot[name]) not in allowed:
                    issues.append(("ERR", f"{name}={lot[name]!r} нет в списке раздела. "
                                          f"Допустимо: {', '.join(sorted(allowed))}"))

    api_error = simulate_funpayapi(lot)
    if api_error:
        issues.append(("ERR", f"FunPayAPI не смог разобрать лот -> {api_error}"))
    return issues


def cmd_validate(args):
    data = load_json(args.file)
    if not isinstance(data, list):
        die("Файл должен содержать JSON-массив лотов: [ {...}, {...} ]")
    if not data:
        die("В файле нет ни одного лота")

    meta = None
    if args.template:
        tpl = load_json(args.template)
        meta_path = str(Path(args.template).with_suffix("")) + ".meta.json"
        if Path(meta_path).exists():
            meta = load_json(meta_path).get("fields") or None
            print(f"ℹ️  Использую метаданные полей из {meta_path}")
        else:
            tpl_lot = tpl[0] if isinstance(tpl, list) and tpl else tpl
            if isinstance(tpl_lot, dict):
                meta = {k: {"required": k in ("fields[summary][ru]", "fields[desc][ru]")}
                        for k in tpl_lot if k.startswith("fields[")}
                print(f"ℹ️  Метаданных нет, проверяю только наличие полей из {args.template}")

    errors = 0
    warnings = 0
    for index, lot in enumerate(data):
        title = (lot.get("fields[summary][ru]") if isinstance(lot, dict) else None) or f"#{index}"
        issues = validate_lot(index, lot, meta)
        for level, message in issues:
            prefix = "❌ ERR " if level == "ERR" else "⚠️  WARN"
            print(f"{prefix} [{index}] {title}: {message}")
        errors += sum(1 for level, _ in issues if level == "ERR")
        warnings += sum(1 for level, _ in issues if level == "WARN")

    print(f"\nВсего лотов: {len(data)} | ошибок: {errors} | предупреждений: {warnings}")
    if errors:
        print("❌ Есть ошибки - такие лоты FunPay не примет или создаст не так, как вы ожидаете.")
        raise SystemExit(2)
    print("✅ Файл выглядит корректно, можно отправлять боту командой /create_lots.")


# --------------------------------------------------------------------------------------
# команда set-node
# --------------------------------------------------------------------------------------

def cmd_set_node(args):
    data = load_json(args.file)
    if not isinstance(data, list) or not data:
        die("В файле должен быть непустой JSON-массив лотов")
    node = str(args.node_id).strip()
    if not node.isdigit():
        die(f"node_id={node!r} должен быть числом (ID раздела из ссылки funpay.com/lots/<ID>/trade)")
    changed = 0
    for lot in data:
        if not isinstance(lot, dict):
            die("В массиве есть элемент, который не является объектом лота")
        if str(lot.get("node_id", "")) != node:
            lot["node_id"] = node
            changed += 1
    out = args.out or args.file
    write_json(out, data)
    print(f"✅ node_id={node} проставлен в {changed} из {len(data)} лотов -> {out}")


# --------------------------------------------------------------------------------------
# команда add-field
# --------------------------------------------------------------------------------------

def resolve_select_value(meta: dict, field: str, value: str) -> str:
    """
    Превращает человекочитаемое значение ("Car Dealership Tycoon") в значение опции формы
    ("1234"), используя метаданные, снятые командой template.
    """
    info = meta.get(field)
    if not info:
        return value  # поле не описано в метаданных - доверяем пользователю
    options = info.get("options") or []
    if not options:
        return value  # это не select: свободный ввод

    exact_value = [v for v, _ in options if v == value]
    if exact_value:
        return value
    exact_label = [v for v, label in options if label.strip().lower() == value.strip().lower()]
    if exact_label:
        return exact_label[0]
    partial = [(v, label) for v, label in options if value.strip().lower() in label.lower()]
    if len(partial) == 1:
        print(f"ℹ️  Нашёл по частичному совпадению: {partial[0][1]!r} = {partial[0][0]!r}")
        return partial[0][0]
    if len(partial) > 1:
        listing = "\n".join(f"  {v} = {label}" for v, label in partial[:15])
        die(f"Значение {value!r} подходит к нескольким опциям поля {field}:\n{listing}\n"
            f"Укажите точное название или сразу значение.")
    die(f"В поле {field} нет опции {value!r}. Доступные значения:\n" +
        "\n".join(f"  {v} = {label}" for v, label in options[:40]))


def cmd_add_field(args):
    data = load_json(args.file)
    if not isinstance(data, list) or not data:
        die("В файле должен быть непустой JSON-массив лотов")

    meta = None
    meta_path = args.meta
    if not meta_path and args.node:
        meta_path = f"template_{args.node}.meta.json"
        if not Path(meta_path).exists():
            die(f"Не найден {meta_path}. Сначала выполните: "
                f"python tools/lot_json_tool.py template {args.node}")
    if meta_path:
        meta = load_json(meta_path).get("fields") or {}
        print(f"ℹ️  Метаданные полей: {meta_path}")

    value = args.value
    if meta and args.field in meta:
        info = meta[args.field]
        print(f"ℹ️  {args.field} — {info.get('label')} "
              f"({'обязательное' if info.get('required') else 'необязательное'})")
        value = resolve_select_value(meta, args.field, value)

    changed = 0
    for lot in data:
        if not isinstance(lot, dict):
            die("В массиве есть элемент, который не является объектом лота")
        if lot.get(args.field) != value:
            lot[args.field] = value
            changed += 1

    out = args.out or args.file
    write_json(out, data)
    print(f"✅ {args.field} = {value!r} проставлено в {changed} из {len(data)} лотов -> {out}")
    print("👉 Теперь проверьте: python tools/lot_json_tool.py validate " + out +
          (f" --template template_{args.node}.json" if args.node else ""))


# --------------------------------------------------------------------------------------
# команда gen
# --------------------------------------------------------------------------------------

def escape_text(value: str) -> str:
    """Превращает литеральные \\n в переносы строк (удобно для CSV из Excel)."""
    return value.replace("\\n", "\n")


def cmd_gen(args):
    template = load_json(args.template)
    if isinstance(template, list):
        if not template:
            die("В шаблоне нет ни одного лота")
        template = template[0]
    if not isinstance(template, dict):
        die("Шаблон должен быть объектом лота (или массивом с одним объектом)")

    rows_path = Path(args.rows)
    if not rows_path.exists():
        die(f"Файл не найден: {rows_path}")

    with rows_path.open("r", encoding=args.encoding, newline="") as f:
        reader = csv.DictReader(f, delimiter=args.delimiter)
        if not reader.fieldnames:
            die("В CSV нет строки с заголовками")
        unknown = [name for name in reader.fieldnames if name and name.strip() not in CSV_COLUMNS]
        if unknown:
            die(f"Неизвестные колонки: {', '.join(unknown)}. Допустимые: {', '.join(CSV_COLUMNS)}")
        rows = list(reader)
    if not rows:
        die("В CSV нет ни одной строки с данными")

    lots = []
    for row_number, row in enumerate(rows, start=2):
        row = {(k or "").strip(): (v or "") for k, v in row.items()}
        if not any(row.values()):
            continue
        lot = dict(template)
        for column, field in CSV_TO_FIELD.items():
            if column not in row:
                continue
            value = row[column].strip()
            if not value:
                continue
            if column == "secrets":
                value = "\n".join(part.strip() for part in value.split("|") if part.strip())
            elif column in ("node_id", "price", "amount", "active", "auto_delivery",
                            "deactivate_after_sale"):
                pass
            else:
                value = escape_text(row[column])
            lot[field] = value

        if not str(lot.get("fields[summary][ru]") or "").strip():
            die(f"Строка {row_number}: пустое summary_ru")
        if not str(lot.get("price") or "").strip():
            die(f"Строка {row_number}: пустой price")
        lots.append(ordered(lot))

    out = args.out or "lots.json"
    write_json(out, lots)
    print(f"✅ Сгенерировано лотов: {len(lots)}")


# --------------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Шаблон полей, генерация и проверка JSON-лотов для плагина Lots Copy (FunPay Cardinal).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_template = sub.add_parser("template", help="выгрузить шаблон полей раздела с FunPay")
    p_template.add_argument("node_id", help="ID раздела (из ссылки https://funpay.com/lots/<ID>/trade)")
    p_template.add_argument("--out", help="куда сохранить JSON (по умолчанию template_<node_id>.json)")
    p_template.add_argument("--config", default=str(DEFAULT_CONFIG), help="путь до configs/_main.cfg")
    p_template.set_defaults(func=cmd_template)

    p_validate = sub.add_parser("validate", help="проверить готовый файл лотов")
    p_validate.add_argument("file", help="JSON-файл для /create_lots")
    p_validate.add_argument("--template", help="шаблон/метаданные раздела для доп. проверок")
    p_validate.set_defaults(func=cmd_validate)

    p_node = sub.add_parser("set-node", help="проставить node_id (ID раздела) во все лоты файла")
    p_node.add_argument("file", help="JSON-файл лотов")
    p_node.add_argument("node_id", help="ID раздела из ссылки https://funpay.com/lots/<ID>/trade")
    p_node.add_argument("--out", help="куда сохранить (по умолчанию перезаписать исходный файл)")
    p_node.set_defaults(func=cmd_set_node)

    p_field = sub.add_parser("add-field", help="добавить/изменить поле во всех лотах файла")
    p_field.add_argument("file", help="JSON-файл лотов")
    p_field.add_argument("field", help='имя поля формы, например "fields[game]"')
    p_field.add_argument("value", help='значение; для select можно писать подпись: "Car Dealership Tycoon"')
    p_field.add_argument("--node", help="ID раздела: метаданные возьмутся из template_<node>.meta.json")
    p_field.add_argument("--meta", help="путь до файла *.meta.json вручную")
    p_field.add_argument("--out", help="куда сохранить (по умолчанию перезаписать исходный файл)")
    p_field.set_defaults(func=cmd_add_field)

    p_gen = sub.add_parser("gen", help="сгенерировать лоты из CSV")
    p_gen.add_argument("--template", required=True, help="базовый лот (JSON из команды template)")
    p_gen.add_argument("--rows", required=True, help="CSV с описаниями/ценами")
    p_gen.add_argument("--out", default="lots.json", help="имя итогового JSON")
    p_gen.add_argument("--delimiter", default=",", help="разделитель CSV (для Excel в RU часто ';')")
    p_gen.add_argument("--encoding", default="utf-8-sig", help="кодировка CSV (Excel: utf-8-sig или cp1251)")
    p_gen.set_defaults(func=cmd_gen)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
