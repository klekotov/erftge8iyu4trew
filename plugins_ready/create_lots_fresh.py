"""
Плагин для FunPay Cardinal: создание лотов по «живой» форме раздела.

Зачем нужен:
    Штатный плагин Lots Copy (/create_lots) берёт ваш JSON и отдаёт его
    FunPayAPI.types.LotFields.renew_fields(). Тот перед отправкой пересобирает форму
    «по-своему»:
      * добавляет "fields[images]" (пустым), даже если в форме раздела такого поля нет;
      * делает цену числом и отправляет "49.0" вместо "49";
      * всегда отправляет поля из файла, даже если в форме раздела их нет;
      * берёт form_created_at из файла (он может быть днями давности), а не свежий.

    А форма лота у каждого раздела своя: например, в разделе 1089 (Battle.net) в лоте
    есть поле "query", в разделе 402 (прочие игры) — поля game / type / method.
    Поэтому «универсальный» набор ключей то работает, то нет.

    Этот плагин работает как сам сайт:
      1. GET lots/offerEdit?node=<node_id> — забирает СВЕЖУЮ форму раздела
         (все input/select/textarea, включая скрытые, свежие csrf_token и form_created_at);
      2. подставляет в неё значения ТОЛЬКО тех полей, которые в этой форме есть;
         для выпадающих списков значение подбирается по подписи варианта
         (например "Car Dealership Tycoon" -> нужный value списка);
      3. POST lots/offerSave — отправляет форму как есть, без пересборки;
      4. если FunPay откажет — показывает дословный текст ошибки FunPay.

Установка:
    1. Меню бота → Плагины → ➕ Добавить плагин → отправить этот файл (.py).
    2. /restart — чтобы плагин загрузился.

Использование (всё из Telegram, консоль на хосте не нужна):
    /fresh_check 402   → какие поля есть в форме раздела, какие обязательные,
                         какие значения у списков. Ничего не создаёт.
    /fresh_raw 402     → один запрос на создание лота и ПОЛНЫЙ разбор ответа FunPay:
                         HTTP-код, адрес, куда перенаправил, JSON или страница,
                         текст, найденные id лотов, что было и что стало в разделе.
    /fresh_bisect 402  → найти виноватое поле: берёт эталон, который уже срабатывал,
                         и добавляет поля вашего лота по одному, пока не сломается.
                         Тестовые лоты удаляются сразу после проверки.
    /fresh_desc 402    → найти максимальную длину описания, которую принимает FunPay:
                         наш текст обрезается до 200/400/600/800/1000/1200/1500 символов,
                         плюс проверка «просто длинный текст без эмодзи».
    /fresh_list 402    → показать ВСЕ ваши лоты раздела (включая неактивные) с id и ценой.
    /fresh_activate 402→ включить (сделать активными) лоты, созданные неактивными.
    /fresh_find [слово]→ просканировать разделы (по умолчанию все, где встречается
                         «roblox» или «dealership») и показать, где у вас вообще есть лоты.
    /fresh_cleanup 402 → удалить тестовые лоты, оставшиеся после /fresh_probe.
    /fresh_probe 402   → диагностика: создаёт по очереди несколько НЕАКТИВНЫХ лотов,
                         каждый со своим набором полей, и показывает, какой вариант
                         FunPay принимает. Нужен, когда непонятно, из-за какого поля
                         приходит «Заполните все поля».
    /fresh_lots        → бот ждёт JSON-файл со лотами (формат как у /create_lots).
"""

from __future__ import annotations

import json
import os
import re
import time
from logging import getLogger
from typing import TYPE_CHECKING

from bs4 import BeautifulSoup

if TYPE_CHECKING:
    from cardinal import Cardinal

from telebot.types import Message
from tg_bot import static_keyboards as skb

NAME = "Create Lots Fresh"
VERSION = "3.1.0"
DESCRIPTION = ("Создает лоты из JSON по свежей форме раздела: отправляет только те поля, "
               "которые есть в форме, и подбирает значения списков. "
               "Решает ошибку «Заполните все поля» в разделах с доп. полями.")
CREDITS = "для FunPay Cardinal"
UUID = "b7f3c1a2-6f4d-4e2b-9a11-7d5c0e8f4b31"
SETTINGS_PAGE = False

logger = getLogger("FPC.create_lots_fresh")
RUNNING = False

CBT_WAIT_FILE = "create_lots_fresh.wait_file"
CBT_BISECT = "create_lots_fresh.bisect"

SAVE_HEADERS = {
    "accept": "*/*",
    "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
    "x-requested-with": "XMLHttpRequest",
}

# Поля, которые всегда берутся из свежей формы, а не из файла:
# это скрытые служебные поля, их нельзя переиспользовать из старого /cache_lots.
SERVER_FIELDS = {"csrf_token", "offer_id", "node_id", "form_created_at", "location", "deleted", "query"}


class OfferForm:
    """Разобранная форма создания лота раздела."""

    def __init__(self, html: str):
        bs = BeautifulSoup(html, "lxml")

        error = bs.find("p", class_="lead")
        if error is not None:
            raise RuntimeError(f"FunPay: {error.text.strip()}")

        form = bs.find("form", class_="form-offer-editor")
        if form is None:
            raise RuntimeError("на странице нет формы лота (неверный node_id или раздел недоступен)")

        self.fields: dict[str, str] = {}
        self.options: dict[str, list[tuple[str, str]]] = {}
        self.required: set[str] = set()
        self.labels: dict[str, str] = {}
        self.hidden: list[str] = []

        for group in form.find_all("div", class_="form-group"):
            label = group.find("label")
            control = group.find(["input", "select", "textarea"])
            if not label or not control or not control.get("name"):
                continue
            self.labels[control["name"]] = label.get_text(strip=True).rstrip("*").strip()
            if ("required" in group.get("class", [])
                    or bool(control.get("required"))
                    or bool(label.find("span", class_="text-danger"))):
                self.required.add(control["name"])

        for inp in form.find_all("input"):
            name = inp.get("name")
            if not name:
                continue
            self.fields[name] = inp.get("value") or ""
            if inp.get("type") == "hidden":
                self.hidden.append(name)

        for ta in form.find_all("textarea"):
            name = ta.get("name")
            if name:
                self.fields[name] = ta.text or ""

        for sel in form.find_all("select"):
            name = sel.get("name")
            if not name:
                continue
            parent = sel.find_parent(class_="form-group")
            if parent and "hidden" in parent.get("class", []):
                continue
            opts = [(o.get("value", ""), o.get_text(strip=True)) for o in sel.find_all("option")]
            self.options[name] = opts
            current = sel.find("option", selected=True) or (sel.find("option") if opts else None)
            self.fields[name] = current.get("value", "") if current is not None else ""

        for cb in form.find_all("input", {"type": "checkbox"}):
            name = cb.get("name")
            if not name:
                continue
            self.fields[name] = "on" if cb.has_attr("checked") else ""

        self.checked = {n for n, v in self.fields.items() if v == "on"}

    def resolve_select(self, name: str, value: str) -> str:
        """
        Подбирает value выпадающего списка по значению или подписи варианта.

        :param name: имя поля.
        :param value: желаемое значение из JSON.

        :raises RuntimeError: если варианта с таким значением нет.
        """
        value = str(value).strip()
        opts = self.options[name]
        if value in {v for v, _ in opts}:
            return value
        lowered = value.casefold()
        for v, text in opts:
            if text.strip().casefold() == lowered:
                return v
        for v, text in opts:
            if lowered and lowered in text.strip().casefold():
                return v
        variants = ", ".join(f"{v or '—'} = {t}" for v, t in opts[:30])
        raise RuntimeError(f"поле «{self.labels.get(name, name)}»: значения {value!r} нет в списке. "
                           f"Доступно: {variants}")

    def describe(self) -> list[str]:
        """Возвращает человекочитаемое описание формы для Telegram."""
        lines = ["🔴 — обязательное, ⚪ — необязательное", ""]
        for name, value in self.fields.items():
            if name in self.hidden:
                continue
            mark = "🔴" if name in self.required else "⚪"
            label = self.labels.get(name, "")
            if name in self.options:
                opts = ", ".join(f"{v or '—'} = {t}" for v, t in self.options[name][:30])
                lines.append(f"{mark} <code>{name}</code> — {label}\n     варианты: {opts}")
            else:
                shown = f"{value!r}" if len(value) <= 40 else f"{len(value)} символов"
                lines.append(f"{mark} <code>{name}</code> — {label} (сейчас: {shown})")
        lines.append("")
        lines.append("🫥 скрытые (берутся из свежей формы): " + ", ".join(self.hidden))
        return lines

    def build(self, lot: dict) -> tuple[dict[str, str], list[str], list[str]]:
        """
        Собирает поля для отправки: свежая форма + значения из JSON.

        :param lot: лот из JSON-файла.

        :return: (поля для отправки, применённые ключи, пропущенные ключи).
        """
        fields = dict(self.fields)
        applied: list[str] = []
        skipped: list[str] = []

        for key, value in lot.items():
            value = "" if value is None else str(value)
            if key in SERVER_FIELDS:
                continue
            if key == "fields[images]":
                # Картинки грузятся отдельно и приходят в форме списком id — из JSON их не берём.
                skipped.append(f"{key} (не переносим)")
                continue
            if key in self.hidden:
                # Скрытые поля (fields[name], fields[images] и т.п.) сайт отправляет ровно
                # теми, что выдал сервер: пользователь их не заполняет. Значения из старого
                # /cache_lots здесь только мешают.
                skipped.append(f"{key} (скрытое, оставил из формы)")
                continue
            if key in self.options:
                try:
                    fields[key] = self.resolve_select(key, value) if value else ""
                except RuntimeError:
                    raise
            elif key in ("active", "auto_delivery", "deactivate_after_sale"):
                fields[key] = "on" if value.strip().casefold() in ("on", "1", "true", "да") else ""
            else:
                fields[key] = value
            applied.append(key)

        # В разделах вида 402 игра выбирается из списка (fields[game]), а fields[name][ru]/[en] —
        # это «своя игра», которую пишут руками. Когда игра выбрана из списка, эти поля должны
        # остаться пустыми (так в ручном лоте из /cache_lots).
        if "fields[game]" in self.options and str(fields.get("fields[game]", "")).strip():
            for name in ("fields[name][ru]", "fields[name][en]"):
                if name in fields and str(fields.get(name, "")).strip():
                    fields[name] = ""
                    skipped.append(f"{name} (очищено: игра выбрана из списка)")

        # Активность: включённый по умолчанию чекбокс не должен «теряться»,
        # если в JSON его вовсе не было.
        if "active" not in lot and "active" in fields:
            fields["active"] = "on"

        for name in self.required:
            if name in SERVER_FIELDS:
                continue
            if not str(fields.get(name, "")).strip():
                raise RuntimeError(f"не заполнено обязательное поле «{self.labels.get(name, name)}» ({name})")

        fields["offer_id"] = "0"
        return fields, applied, skipped


def describe_form(html: str) -> list[str]:
    """Совместимость: описание формы."""
    return OfferForm(html).describe()


def response_text(response) -> str:
    """
    Возвращает читаемый текст ответа FunPay (без HTML-разметки).

    :param response: объект ответа requests.
    """
    body = response.text or ""
    try:
        text = BeautifulSoup(body, "lxml").get_text(" ", strip=True)
    except Exception:
        text = body
    text = re.sub(r"\s+", " ", text).strip()
    return text[:400]


def parse_error(payload: dict) -> str | None:
    """
    Достает текст ошибки из JSON-ответа FunPay.

    :param payload: JSON-ответ.

    :return: текст ошибки или None.
    """
    error = payload.get("error") or payload.get("msg")
    if not error and payload.get("errors"):
        errors = payload["errors"]
        if isinstance(errors, list):
            error = "; ".join(f"{name}: {text}" for name, text in errors)
        else:
            error = str(errors)
    return str(error) if error else None


def save_offer(cardinal: Cardinal, fields: dict[str, str]) -> tuple[dict, object, bool]:
    """
    Отправляет форму на FunPay без пересборки полей (как это делает сайт).

    :param cardinal: объект кардинала.
    :param fields: поля формы.

    :return: (JSON-ответ или {}, объект ответа, был ли ответ JSON).

    :raises RuntimeError: если FunPay вернул ошибку.
    """
    fields = dict(fields)
    fields.setdefault("offer_id", "0")

    response = cardinal.account.method("post", "lots/offerSave", SAVE_HEADERS, fields, raise_not_200=True)

    is_json = True
    try:
        payload = response.json()
        if not isinstance(payload, dict):
            payload, is_json = {}, False
    except Exception:
        payload, is_json = {}, False

    error = parse_error(payload)
    if error:
        raise RuntimeError(error)
    return payload, response, is_json


def lot_ids(cardinal: Cardinal, node_id: str) -> set[int]:
    """Возвращает id своих лотов раздела (для проверки «до/после»)."""
    try:
        return {int(lot.id) for lot in my_lots(cardinal, node_id)}
    except Exception:
        return set()


def save_and_verify(cardinal: Cardinal, node_id: str, fields: dict[str, str],
                    title: str, price: str, before_ids: set[int] | None = None) -> tuple[bool, int | None, str]:
    """
    Сохраняет лот и проверяет по списку лотов раздела, что он действительно создан.

    :param cardinal: объект кардинала.
    :param node_id: id раздела.
    :param fields: поля формы.
    :param title: ожидаемый заголовок лота.
    :param price: ожидаемая цена.
    :param before_ids: id лотов, существовавших до создания.

    :return: (создан ли лот, id лота, сообщение об ошибке).
    """
    payload, response, is_json = save_offer(cardinal, fields)

    offer_id = extract_offer_id(payload) if payload else None
    if offer_id:
        return True, offer_id, ""

    # FunPay может ответить пустым JSON или HTML — тогда доверять нечему,
    # проверяем по факту: появился ли лот в разделе.
    try:
        found = lot_is_visible(my_lots(cardinal, node_id), title, price, before_ids)
    except Exception as e:
        if payload:
            return True, None, f"создан, но проверить не удалось ({e})"
        return False, None, f"ответ FunPay: {response_text(response)}"

    if found:
        return True, found, ""

    if is_json:
        return False, None, (f"FunPay вернул пустой ответ (HTTP {response.status_code}, "
                             f"{str(payload) or '{}'}) и лот в разделе не появился")
    return False, None, (f"FunPay вернул не JSON, а страницу (HTTP {response.status_code}, "
                         f"{response.url}). Текст со страницы: {response_text(response)}")


def create_lot_with_fallback(cardinal: Cardinal, node_id: str, fields: dict[str, str],
                              title: str, price: str) -> tuple[bool, int | None, str, bool, list[str]]:
    """
    Создает лот, перебирая варианты, если FunPay отказывает.

    Порядок попыток (первая успешная — конец):
      1. как есть (с активностью из файла);
      2. без активации (active = "");
      3. без активации и без поля «Название игры» (fields[name][ru]/[en]).

    Отказ FunPay бывает двух видов: JSON с ошибкой (тогда save_offer возбуждает
    RuntimeError) и HTML-страница вместо JSON (тогда save_and_verify возвращает False).
    Обрабатываются оба.

    :return: (успех, id лота, сообщение об ошибке, создан ли неактивным, лог попыток).
    """
    attempts: list[str] = []
    variants: list[tuple[str, dict[str, str], bool]] = [("как в файле", dict(fields), False)]

    if str(fields.get("active", "")).strip():
        v = dict(fields); v["active"] = ""
        variants.append(("без активации", v, True))

    v = dict(fields); v["active"] = ""
    for name in ("fields[name][ru]", "fields[name][en]"):
        if name in v:
            v[name] = ""
    variants.append(("без активации и без «Название игры»", v, True))

    last_message = ""
    for label, variant_fields, is_inactive in variants:
        before_ids = lot_ids(cardinal, node_id)
        try:
            ok, offer_id, message = save_and_verify(cardinal, node_id, variant_fields, title,
                                                    variant_fields.get("price", "0"), before_ids)
        except Exception as e:
            ok, offer_id, message = False, None, str(e)

        if ok:
            if attempts:
                attempts.append(f"{label} — ✅")
            return True, offer_id, "", is_inactive, attempts
        attempts.append(f"{label} — ❌ {message}")
        last_message = message

    return False, None, last_message, False, attempts


def download_file(tg, msg: Message, file_name: str = "fresh_lots.json") -> bool:
    """Скачивает присланный файл в storage/cache/."""
    tg.bot.send_message(msg.chat.id, "⏬ Загружаю файл...")
    try:
        file_info = tg.bot.get_file(msg.document.file_id)
        content = tg.bot.download_file(file_info.file_path)
    except Exception:
        tg.bot.send_message(msg.chat.id, "❌ Произошла ошибка при загрузке файла.")
        logger.debug("TRACEBACK", exc_info=True)
        return False

    with open(f"storage/cache/{file_name}", "wb") as f:
        f.write(content)
    return True



PROBE_TEXT = {
    "summary_ru": "🟪🟣Volkswagen Golf GTI Racer Edition💜🟣1,033 RAP💜🟪БЫСТРО",
    "summary_en": "🟪🟣Volkswagen Golf TDI Racer Edition💜🟣1,033 RAP💜🟪FAST",
    "desc_ru": ("🚗 Машина: Volkswagen Golf GTI Racer Edition\n"
                "💰 Валюта: 1,033 RAP\n\n"
                "🚧 ВАЖНО: для трейда нужен автосалон, прокачанный на 100%.\n"
                "Напишите мне ДО покупки — подскажу, как это сделать.\n\n"
                "⏱ Доставка 5–15 минут. 🎁 Скидка 10% за отзыв."),
    "desc_en": ("🚗 Car: Volkswagen Golf TDI Racer Edition\n"
                "💰 Amount: 1,033 RAP\n\n"
                "🚧 IMPORTANT: trading requires a 100% completed car dealership.\n"
                "Message me BEFORE buying and I will explain how to do it.\n\n"
                "⏱ Delivery 5–15 minutes. 🎁 10% discount for a review."),
    "payment_ru": ("Спасибо за покупку! 🚧 Напоминаю: для трейда нужен автосалон на 100%.\n"
                   "Напишите ваш ник и название машины — выдам в течение 5–15 минут."),
    "payment_en": ("Thank you for your purchase! 🚧 Reminder: trading requires a 100% car dealership.\n"
                   "Send me your nickname and the car name — delivery in 5–15 minutes."),
}

PROBE_FILLER = "sdf43w0irt430pitrp943utiou33wuer4thw4twr"


def probe_variants() -> list[tuple[str, dict[str, str]]]:
    """
    Набор вариантов для диагностики: каждый — набор значений поверх формы раздела.

    Первые два варианта повторяют вручную созданный лот из /cache_lots (он точно
    проходил валидацию FunPay), дальше по одному меняется подозрительное поле.

    :return: список пар (описание варианта, значения полей).
    """
    t = PROBE_TEXT
    his = {  # точная копия вручную созданного лота раздела 402
        "fields[name][ru]": "", "fields[name][en]": "",
        "fields[summary][ru]": PROBE_FILLER, "fields[summary][en]": PROBE_FILLER,
        "fields[desc][ru]": PROBE_FILLER, "fields[desc][en]": PROBE_FILLER,
        "fields[payment_msg][ru]": PROBE_FILLER, "fields[payment_msg][en]": PROBE_FILLER,
        "price": "49", "amount": "1", "active": "",
        "fields[game]": "Car Dealership Tycoon", "fields[type]": "Предметы",
        "fields[method]": "Трейд",
    }
    ours = {
        "fields[name][ru]": "", "fields[name][en]": "",
        "fields[summary][ru]": t["summary_ru"], "fields[summary][en]": t["summary_en"],
        "fields[desc][ru]": t["desc_ru"], "fields[desc][en]": t["desc_en"],
        "fields[payment_msg][ru]": t["payment_ru"], "fields[payment_msg][en]": t["payment_en"],
        "price": "49", "amount": "1", "active": "",
        "fields[game]": "Car Dealership Tycoon", "fields[type]": "Предметы",
        "fields[method]": "Трейд",
    }

    variants = [
        ("1. как ваш ручной лот: заполнители, без названия, + method", dict(his)),
        ("2. наши тексты, название игры пустое, + method", dict(ours)),
    ]

    v = dict(ours); v["fields[name][ru]"] = v["fields[name][en]"] = "Volkswagen Golf GTI Racer Edition"
    variants.append(("3. наши тексты + название машины в «Название игры»", v))

    v = dict(ours)
    v["fields[desc][ru]"] = v["fields[desc][en]"] = "тест"
    v["fields[payment_msg][ru]"] = v["fields[payment_msg][en]"] = ""
    variants.append(("4. наши тексты, описание в одну строку", v))

    v = dict(ours)
    v["fields[summary][ru]"] = "Volkswagen Golf GTI Racer Edition 1033 RAP БЫСТРО"
    v["fields[summary][en]"] = "Volkswagen Golf TDI Racer Edition 1033 RAP FAST"
    variants.append(("5. наши тексты, заголовок без эмодзи", v))

    v = dict(ours); v.pop("fields[method]", None)
    variants.append(("6. наши тексты, БЕЗ fields[method]", v))

    v = dict(ours); v["fields[type]"] = "Игровая валюта"
    variants.append(("7. наши тексты, тип «Игровая валюта»", v))

    v = dict(ours); v["fields[game]"] = ""; v["fields[name][ru]"] = v["fields[name][en]"] = "Car Dealership Tycoon"
    variants.append(("8. игра не выбрана, название игры = Car Dealership Tycoon", v))

    return variants


PROBE_TITLES = {
    PROBE_FILLER,
    PROBE_TEXT["summary_ru"],
    PROBE_TEXT["summary_en"],
    "Volkswagen Golf GTI Racer Edition 1033 RAP БЫСТРО",
    "Volkswagen Golf TDI Racer Edition 1033 RAP FAST",
}
"""Заголовки лотов, которые создаёт /fresh_probe (по ним /fresh_cleanup их и находит)."""


def extract_offer_id(payload: dict) -> int | None:
    """Пытается достать id созданного лота из ответа FunPay."""
    for key in ("id", "offer_id", "offerId", "lotId", "lot_id"):
        value = str(payload.get(key, ""))
        if value.isdigit() and value != "0":
            return int(value)
    url = str(payload.get("url") or payload.get("redirect") or "")
    match = re.search(r"[?&]id=(\d+)", url)
    if match:
        return int(match.group(1))
    return None


def my_lots(cardinal: Cardinal, node_id: str):
    """
    Возвращает список своих лотов в разделе.

    :param cardinal: объект кардинала.
    :param node_id: id раздела.

    :return: список MyLotShortcut.

    :raises RuntimeError: если список получить не удалось.
    """
    try:
        return cardinal.account.get_my_subcategory_lots(int(node_id))
    except Exception as e:
        raise RuntimeError(f"не удалось получить список лотов раздела {node_id}: {e}") from e


def find_same_title(cardinal: Cardinal, node_id: str, title: str):
    """
    Ищет в разделе лот с таким же заголовком (FunPay не любит дубли в одном разделе).

    :return: объект лота с таким же заголовком или None.
    """
    probe = (title or "").strip()
    if not probe:
        return None
    try:
        for lot in my_lots(cardinal, node_id):
            if (lot.description or "").strip() == probe:
                return lot
    except Exception:
        logger.debug("[FRESH LOTS] Не удалось проверить дубликаты.", exc_info=True)
    return None


def is_probe_leftover(lot) -> bool:
    """Похож ли лот на тестовый, оставленный /fresh_probe (неактивный, 49 ₽, наш заголовок)."""
    try:
        return (not lot.active
                and abs(float(lot.price or 0) - 49) <= 0.01
                and (lot.description or "").strip() in PROBE_TITLES)
    except Exception:
        return False


def lot_is_visible(lots, title: str, price: str, exclude: set[int] | None = None) -> int | None:
    """
    Ищет созданный лот в списке своих лотов раздела.

    :param lots: список лотов раздела.
    :param title: заголовок лота.
    :param price: цена лота (строкой).
    :param exclude: id лотов, которые уже были до создания (их не считаем).

    :return: id найденного лота или None.
    """
    probe = (title or "").strip()[:25]
    try:
        price_f = float(str(price).replace(",", "."))
    except (TypeError, ValueError):
        price_f = None
    for lot in lots:
        if exclude and int(lot.id) in exclude:
            continue
        desc = (lot.description or "").strip()
        if probe and probe not in desc:
            continue
        if price_f is not None and abs(float(lot.price or 0) - price_f) > 0.01:
            continue
        return int(lot.id)
    return None


def delete_lot_raw(cardinal: Cardinal, form_fields: dict[str, str], offer_id: int) -> bool:
    """Удаляет лот по id (сырым запросом, без пересборки формы)."""
    try:
        fields = dict(form_fields)
        fields.update({"offer_id": str(offer_id), "deleted": "1"})
        cardinal.account.method("post", "lots/offerSave", SAVE_HEADERS, fields, raise_not_200=True)
        return True
    except Exception:
        logger.warning(f"[FRESH BISECT] Не удалось удалить тестовый лот {offer_id}.")
        logger.debug("TRACEBACK", exc_info=True)
        return False


def make_ladder(lot: dict, form_fields: dict[str, str]) -> list[tuple[str, dict[str, str]]]:
    """
    Строит «лестницу»: от эталона, который гарантированно срабатывал, к нашему лоту,
    добавляя по одному полю.

    Эталон — вариант 2 из /fresh_probe: короткие тексты, цена 49, выключенный лот.
    Именно он создавал лоты в разделе 402.
    """
    base = dict(form_fields)
    base.update({
        "fields[name][ru]": "", "fields[name][en]": "",
        "fields[summary][ru]": PROBE_TEXT["summary_ru"],
        "fields[summary][en]": PROBE_TEXT["summary_en"],
        "fields[desc][ru]": PROBE_TEXT["desc_ru"],
        "fields[desc][en]": PROBE_TEXT["desc_en"],
        "fields[payment_msg][ru]": PROBE_TEXT["payment_ru"],
        "fields[payment_msg][en]": PROBE_TEXT["payment_en"],
        "price": "49", "amount": "1", "active": "",
    })
    for key in ("fields[game]", "fields[type]", "fields[method]"):
        if lot.get(key):
            base[key] = str(lot[key])

    steps: list[tuple[str, dict[str, str]]] = [
        ("0. эталон (срабатывал в /fresh_probe)", dict(base)),
    ]

    current = dict(base)
    order = [
        ("1. + цена из файла", ["price"]),
        ("2. + наш заголовок", ["fields[summary][ru]", "fields[summary][en]"]),
        ("3. + наше описание", ["fields[desc][ru]", "fields[desc][en]"]),
        ("4. + наше сообщение покупателю", ["fields[payment_msg][ru]", "fields[payment_msg][en]"]),
        ("5. + «Название игры» из файла", ["fields[name][ru]", "fields[name][en]"]),
    ]
    for label, keys in order:
        nxt = dict(current)
        for key in keys:
            if key in lot:
                nxt[key] = str(lot[key])
        current = nxt
        steps.append((label, dict(current)))

    # Если дело в длине текста — проверим укороченные варианты.
    for limit in (1200, 800, 400):
        v = dict(base)
        v["fields[desc][ru]"] = str(lot.get("fields[desc][ru]", ""))[:limit]
        v["fields[desc][en]"] = str(lot.get("fields[desc][en]", ""))[:limit]
        v["fields[payment_msg][ru]"] = str(lot.get("fields[payment_msg][ru]", ""))[:limit]
        v["fields[payment_msg][en]"] = str(lot.get("fields[payment_msg][en]", ""))[:limit]
        v["fields[summary][ru]"] = str(lot.get("fields[summary][ru]", ""))
        v["fields[summary][en]"] = str(lot.get("fields[summary][en]", ""))
        v["price"] = str(lot.get("price", "49"))
        steps.append((f"6. наши тексты, обрезанные до {limit} символов", v))

    return steps


def init_commands(cardinal: Cardinal):
    if not cardinal.telegram:
        return
    tg = cardinal.telegram
    bot = tg.bot

    def get_form(node_id: str) -> OfferForm:
        """Забирает и разбирает свежую форму создания лота раздела."""
        response = cardinal.account.method("get", f"lots/offerEdit?node={node_id}", {}, {},
                                           raise_not_200=True)
        return OfferForm(response.content.decode())

    def fresh_check(m: Message):
        args = (m.text or "").split()
        node_id = args[1].strip() if len(args) > 1 else ""
        if not node_id.isdigit():
            bot.send_message(m.chat.id, "Использование: <code>/fresh_check 402</code>")
            return
        try:
            form = get_form(node_id)
        except Exception as e:
            bot.send_message(m.chat.id, f"❌ Не удалось получить форму раздела {node_id}: <b>{e}</b>")
            logger.debug("TRACEBACK", exc_info=True)
            return

        text = (f"Форма раздела <a href=\"https://funpay.com/lots/{node_id}/trade\">{node_id}</a>\n\n"
                + "\n".join(form.describe()))
        for i in range(0, len(text), 3500):
            bot.send_message(m.chat.id, text[i:i + 3500])


    def fresh_probe(m: Message):
        args = (m.text or "").split()
        node_id = args[1].strip() if len(args) > 1 else "402"
        if not node_id.isdigit():
            bot.send_message(m.chat.id, "Использование: <code>/fresh_probe 402</code>")
            return
        try:
            form = get_form(node_id)
        except Exception as e:
            bot.send_message(m.chat.id, f"❌ Не удалось получить форму раздела {node_id}: <b>{e}</b>")
            return

        variants = probe_variants()
        bot.send_message(m.chat.id,
                         f"🔬 Диагностика раздела {node_id}: {len(variants)} вариантов по 1 лоту, "
                         f"все <b>неактивные</b> и с ценой 49 ₽.\n"
                         f"Занимает ~{len(variants) + 2} секунд.")

        results: list[str] = []
        winner: str | None = None
        for label, values in variants:
            fields = dict(form.fields)
            fields.update(values)
            fields["offer_id"] = "0"
            if "amount" not in values:
                fields["amount"] = "1"
            title = str(values.get("fields[summary][ru]") or "")
            price = str(values.get("price") or "49")
            try:
                time.sleep(1)
                before_ids = lot_ids(cardinal, node_id)
                ok, offer_id, message = save_and_verify(cardinal, node_id, fields, title, price, before_ids)
                if not ok:
                    results.append(f"❌ <b>{label}</b>\n     {message}")
                    logger.warning(f"[FRESH PROBE] Вариант не прошёл: {label} ({message})")
                    continue
                results.append(f"✅ <b>{label}</b> — создан" + (f", id {offer_id}" if offer_id else ""))
                winner = winner or label
                logger.info(f"[FRESH PROBE] Вариант прошёл: {label} (id={offer_id})")
                if offer_id:
                    try:
                        delete_fields = dict(form.fields)
                        delete_fields.update({"offer_id": str(offer_id), "deleted": "1"})
                        cardinal.account.method("post", "lots/offerSave", SAVE_HEADERS,
                                                delete_fields, raise_not_200=True)
                        results.append("     ↳ тестовый лот сразу удалён")
                    except Exception:
                        results.append("     ↳ создан, удалить автоматически не смог — удалите вручную")
            except Exception as e:
                results.append(f"❌ <b>{label}</b>\n     FunPay: {e}")
                logger.warning(f"[FRESH PROBE] Вариант не прошёл: {label} ({e})")

        text = "🔬 <b>Результат диагностики</b>\n\n" + "\n\n".join(results)
        if winner:
            text += f"\n\n🎯 Рабочий вариант: <b>{winner}</b>. Пришлите этот текст мне — поправлю плагин."
        else:
            text += ("\n\n😕 Ни один вариант не прошёл — дело не в значениях полей, "
                     "а в самом запросе. Пришлите скриншот.")
        for i in range(0, len(text), 3500):
            bot.send_message(m.chat.id, text[i:i + 3500])

    def fresh_cleanup(m: Message):
        args = (m.text or "").split()
        node_id = args[1].strip() if len(args) > 1 else "402"
        if not node_id.isdigit():
            bot.send_message(m.chat.id, "Использование: <code>/fresh_cleanup 402</code>")
            return
        try:
            lots = cardinal.account.get_my_subcategory_lots(int(node_id))
        except Exception as e:
            bot.send_message(m.chat.id, f"❌ Не удалось получить список лотов раздела {node_id}: <b>{e}</b>")
            logger.debug("TRACEBACK", exc_info=True)
            return

        victims = []
        for lot in lots:
            title = (lot.description or "").strip()
            if title not in PROBE_TITLES:
                continue
            if lot.active:
                continue
            if abs(float(lot.price or 0) - 49) > 0.01:
                continue
            victims.append(lot)

        if not victims:
            bot.send_message(m.chat.id, "🧹 Тестовых лотов не найдено — удалять нечего.")
            return

        deleted = 0
        for lot in victims:
            try:
                time.sleep(1)
                cardinal.account.delete_lot(int(lot.id))
                deleted += 1
                logger.info(f"[FRESH CLEANUP] Удалил тестовый лот {lot.id} ({lot.description}).")
            except Exception:
                logger.warning(f"[FRESH CLEANUP] Не удалось удалить лот {lot.id}.")
                logger.debug("TRACEBACK", exc_info=True)

        bot.send_message(m.chat.id, f"🧹 Нашёл тестовых лотов: <b>{len(victims)}</b>, удалил: <b>{deleted}</b>.")

    def fresh_list(m: Message):
        args = (m.text or "").split()
        node_id = args[1].strip() if len(args) > 1 else "402"
        if not node_id.isdigit():
            bot.send_message(m.chat.id, "Использование: <code>/fresh_list 402</code>")
            return
        try:
            lots = my_lots(cardinal, node_id)
        except RuntimeError as e:
            bot.send_message(m.chat.id, f"❌ {e}")
            logger.debug("TRACEBACK", exc_info=True)
            return

        if not lots:
            bot.send_message(m.chat.id,
                             f"В разделе <a href=\"https://funpay.com/lots/{node_id}/trade\">{node_id}</a> "
                             f"у вас нет ни одного лота — ни активного, ни неактивного.")
            return

        active = [l for l in lots if l.active]
        lines = [f"Лоты в разделе <a href=\"https://funpay.com/lots/{node_id}/trade\">{node_id}</a>: "
                 f"всего <b>{len(lots)}</b>, активных <b>{len(active)}</b>, "
                 f"неактивных <b>{len(lots) - len(active)}</b>", ""]
        for lot in lots[:60]:
            mark = "🟢" if lot.active else "⚪"
            title = (lot.description or "без описания").strip()
            lines.append(f"{mark} id <code>{lot.id}</code> · {lot.price:g} ₽ · {title[:70]}")
        bot.send_message(m.chat.id, "\n".join(lines))

    def fresh_raw(m: Message):
        args = (m.text or "").split()
        node_id = args[1].strip() if len(args) > 1 else "402"
        if not node_id.isdigit():
            bot.send_message(m.chat.id, "Использование: <code>/fresh_raw 402</code>")
            return
        try:
            form = get_form(node_id)
        except Exception as e:
            bot.send_message(m.chat.id, f"❌ Не удалось получить форму раздела {node_id}: <b>{e}</b>")
            return

        label, values = probe_variants()[1]  # наши тексты, название игры пустое
        fields = dict(form.fields)
        fields.update(values)
        fields["offer_id"] = "0"
        if "amount" not in values:
            fields["amount"] = "1"

        before = lot_ids(cardinal, node_id)
        bot.send_message(m.chat.id, f"🧪 Отправляю ОДИН лот в раздел {node_id} (неактивный, 49 ₽) "
                                    f"и показываю полный ответ FunPay...")

        try:
            response = cardinal.account.method("post", "lots/offerSave", SAVE_HEADERS, fields,
                                               raise_not_200=True)
        except Exception as e:
            bot.send_message(m.chat.id, f"❌ Запрос не прошёл: <b>{e}</b>")
            logger.debug("TRACEBACK", exc_info=True)
            return

        after = lot_ids(cardinal, node_id)
        new_ids = sorted(after - before)

        is_json = True
        try:
            payload = response.json()
            if not isinstance(payload, dict):
                payload, is_json = {}, False
        except Exception:
            payload, is_json = {}, False

        history = " → ".join(f"{r.status_code} {r.url}" for r in response.history) or "без редиректов"
        found_ids = set(re.findall(r"[?&]id=(\d+)", response.text or ""))
        found_ids |= set(re.findall(r'data-offer="(\d+)"', response.text or ""))
        body = (response.text or "")[:600]

        lines = [
            f"🔍 <b>Ответ FunPay на создание лота в разделе {node_id}</b>",
            "",
            f"HTTP: <b>{response.status_code}</b>",
            f"Адрес ответа: <code>{response.url}</code>",
            f"Редиректы: {history}",
            f"Content-Type: <code>{response.headers.get('content-type', '—')}</code>",
            f"JSON: <b>{'да' if is_json else 'НЕТ, это HTML-страница'}</b>",
        ]
        if is_json:
            lines.append(f"JSON: <code>{str(payload)[:400]}</code>")
            err = parse_error(payload)
            lines.append(f"Ошибка в JSON: <b>{err or 'нет'}</b>")
        else:
            lines.append(f"Текст страницы: <code>{response_text(response)[:400]}</code>")

        lines += [
            "",
            f"Лотов в разделе было: <b>{len(before)}</b>, стало: <b>{len(after)}</b>",
            f"Новые id: <b>{', '.join(map(str, new_ids)) or 'нет'}</b>",
            f"id лотов, найденные в ответе: <b>{', '.join(sorted(found_ids)) or 'нет'}</b>",
            "",
            "Если новых id нет — лот не создан нигде. Если есть — он лежит в разделе: "
            f"<code>/fresh_list {node_id}</code>",
        ]
        text = "\n".join(lines)
        for i in range(0, len(text), 3500):
            bot.send_message(m.chat.id, text[i:i + 3500])

    def fresh_find(m: Message):
        args = (m.text or "").split()
        word = args[1].strip().casefold() if len(args) > 1 else ""
        keywords = [word] if word else ["roblox", "dealership", "tycoon"]

        try:
            subcategories = cardinal.account.subcategories
        except Exception as e:
            bot.send_message(m.chat.id, f"❌ Не удалось получить список разделов: <b>{e}</b>")
            return

        targets = []
        for sub in subcategories:
            # Только обычные лоты (не лоты игровой валюты).
            if getattr(sub.type_, "name", "") != "COMMON":
                continue
            category = getattr(sub, "category", None)
            haystack = f"{sub.name} {getattr(category, 'name', '')}".casefold()
            if any(k in haystack for k in keywords):
                targets.append(sub)

        # раздел из команды (/fresh_find 402) — всегда в списке
        for arg in args[1:]:
            if arg.isdigit():
                for sub in subcategories:
                    if int(sub.id_) == int(arg) and sub not in targets:
                        targets.append(sub)
                        keywords = []

        if not targets:
            bot.send_message(m.chat.id, "😕 Подходящих разделов не нашлось. "
                                        "Укажите слово или id: <code>/fresh_find 402</code>")
            return

        bot.send_message(m.chat.id,
                         f"🔎 Проверяю ваши лоты в {len(targets)} разделах "
                         f"(по слову: {', '.join(keywords) or 'id из команды'}).\n"
                         f"Это займёт ~{int(len(targets) * 0.7) + 2} секунд...")

        found_lines: list[str] = []
        checked = 0
        for sub in targets:
            checked += 1
            try:
                time.sleep(0.4)
                lots = cardinal.account.get_my_subcategory_lots(int(sub.id_))
            except Exception:
                logger.debug(f"[FRESH FIND] Не удалось получить лоты раздела {sub.id_}.", exc_info=True)
                continue
            if not lots:
                continue
            found_lines.append(f"\n📦 <b>{sub.name}</b> "
                               f"(<a href=\"https://funpay.com/lots/{sub.id_}/trade\">{sub.id_}</a>) — "
                               f"{len(lots)} лот(-ов)")
            for lot in lots[:25]:
                mark = "🟢" if lot.active else "⚪"
                found_lines.append(f"  {mark} id <code>{lot.id}</code> · {lot.price:g} ₽ · "
                                   f"{(lot.description or '')[:60]}")

        if not found_lines:
            bot.send_message(m.chat.id,
                             f"Проверил {checked} разделов — <b>лотов нет ни в одном</b>.\n"
                             f"Значит /fresh_probe действительно ничего не создал.")
            return

        text = (f"🔎 Проверено разделов: {checked}.\nНайдены ваши лоты:" + "\n".join(found_lines))
        for i in range(0, len(text), 3500):
            bot.send_message(m.chat.id, text[i:i + 3500])

    def fresh_activate(m: Message):
        args = (m.text or "").split()
        node_id = args[1].strip() if len(args) > 1 else "402"
        confirm = len(args) > 2 and args[2].strip().lower() in ("yes", "да", "ok")
        if not node_id.isdigit():
            bot.send_message(m.chat.id, "Использование: <code>/fresh_activate 402</code>")
            return
        try:
            lots = my_lots(cardinal, node_id)
        except RuntimeError as e:
            bot.send_message(m.chat.id, f"❌ {e}")
            return

        inactive = [l for l in lots if not l.active]
        if not inactive:
            bot.send_message(m.chat.id, "✅ В разделе нет неактивных лотов — включать нечего.")
            return

        if not confirm:
            lines = [f"Нашёл неактивных лотов: <b>{len(inactive)}</b>", ""]
            for lot in inactive[:40]:
                lines.append(f"⚪ id <code>{lot.id}</code> · {lot.price:g} ₽ · "
                             f"{(lot.description or '')[:60]}")
            lines += ["", f"Чтобы включить их все: <code>/fresh_activate {node_id} yes</code>"]
            bot.send_message(m.chat.id, "\n".join(lines))
            return

        activated = 0
        for lot in inactive:
            try:
                time.sleep(1)
                response = cardinal.account.method(
                    "get", f"lots/offerEdit?node={node_id}&offer={lot.id}", {}, {}, raise_not_200=True)
                form = OfferForm(response.content.decode())
                fields = dict(form.fields)
                fields["offer_id"] = str(lot.id)
                fields["active"] = "on"
                payload, resp, is_json = save_offer(cardinal, fields)
                activated += 1
                logger.info(f"[FRESH LOTS] Включил лот {lot.id}.")
            except Exception as e:
                logger.warning(f"[FRESH LOTS] Не удалось включить лот {lot.id}: {e}")
                bot.send_message(m.chat.id, f"❌ Не удалось включить лот <code>{lot.id}</code>: <b>{e}</b>")

        bot.send_message(m.chat.id, f"🔆 Включил лотов: <b>{activated}</b> из <b>{len(inactive)}</b>. "
                                    f"Проверьте: <code>/fresh_list {node_id}</code>")

    def act_bisect(m: Message):
        result = bot.send_message(m.chat.id,
                                  "Отправьте JSON со лотами. Возьму <b>первый</b> лот и проверю, "
                                  "какое именно поле вызывает «Заполните все поля».\n\n"
                                  "Каждый тестовый лот создаётся выключенным и сразу удаляется.",
                                  reply_markup=skb.CLEAR_STATE_BTN())
        tg.set_state(m.chat.id, result.id, m.from_user.id, CBT_BISECT)

    def bisect(m: Message):
        tg.clear_state(m.chat.id, m.from_user.id, True)
        if not m.document or not m.document.file_name.endswith(".json"):
            bot.send_message(m.chat.id, "❌ Это не JSON-файл.")
            return
        if not download_file(tg, m, "bisect_lots.json"):
            return
        try:
            with open("storage/cache/bisect_lots.json", "r", encoding="utf-8") as f:
                data = json.loads(f.read())
        except Exception as e:
            bot.send_message(m.chat.id, f"❌ Не удалось прочитать файл: <b>{e}</b>")
            return

        if isinstance(data, dict):
            data = [data]
        lot = next((x for x in data if isinstance(x, dict)), None)
        if not lot:
            bot.send_message(m.chat.id, "❌ В файле нет лотов.")
            return

        node_id = str(lot.get("node_id") or "402").strip()
        if not node_id.isdigit():
            bot.send_message(m.chat.id, "❌ Не указан node_id.")
            return

        try:
            form = get_form(node_id)
        except Exception as e:
            bot.send_message(m.chat.id, f"❌ Не удалось получить форму раздела {node_id}: <b>{e}</b>")
            return

        steps = make_ladder(lot, form.fields)
        title = str(lot.get("fields[summary][ru]") or "тест")
        bot.send_message(m.chat.id,
                         f"🔬 Проверяю {len(steps)} вариантов на лоте <code>{title[:40]}</code>.\n"
                         f"Каждый тестовый лот удаляется сразу. ~{len(steps) * 3} секунд...")

        results: list[str] = []
        first_bad: str | None = None
        for label, fields in steps:
            fields = dict(fields)
            fields["offer_id"] = "0"
            price = str(fields.get("price") or "49")
            step_title = str(fields.get("fields[summary][ru]") or "")
            before = lot_ids(cardinal, node_id)
            time.sleep(1)
            try:
                ok, offer_id, message = save_and_verify(cardinal, node_id, fields, step_title,
                                                        price, before)
            except Exception as e:
                ok, offer_id, message = False, None, str(e)

            if ok:
                results.append(f"✅ {label}")
                if offer_id:
                    time.sleep(0.5)
                    delete_lot_raw(cardinal, form.fields, offer_id)
                else:
                    new_ids = sorted(lot_ids(cardinal, node_id) - before)
                    for new_id in new_ids:
                        delete_lot_raw(cardinal, form.fields, new_id)
            else:
                results.append(f"❌ <b>{label}</b>\n     {message}")
                if first_bad is None:
                    first_bad = label
                    # не останавливаемся: остальные шаги (например, обрезка текста)
                    # тоже полезны — по ним видно, где именно проходит.

        text = ("🔬 <b>Где ломается</b>\n\n" + "\n".join(results))
        if first_bad and first_bad.startswith("0"):
            text += "\n\n😕 Не сработал даже эталон — значит изменилось на стороне FunPay (лимит/модерация)."
        elif first_bad:
            text += (f"\n\n🎯 Виновато: <b>{first_bad}</b> — всё, что до него, создаётся. "
                     f"Пришлите этот экран, поправлю файл или плагин под это.")
        else:
            text += "\n\n🎉 Все варианты создались — пришлите экран, разберёмся, что поменялось."
        for i in range(0, len(text), 3500):
            bot.send_message(m.chat.id, text[i:i + 3500])

    def fresh_desc(m: Message):
        args = (m.text or "").split()
        node_id = args[1].strip() if len(args) > 1 else "402"
        if not node_id.isdigit():
            bot.send_message(m.chat.id, "Использование: <code>/fresh_desc 402</code>")
            return

        # берем описание из файла, который прислал пользователь последним
        source = "storage/cache/fresh_lots.json"
        if not os.path.exists(source):
            bot.send_message(m.chat.id, "Сначала пришлите JSON через <code>/fresh_lots</code> "
                                        "(файл нужен как образец текста).")
            return
        try:
            with open(source, "r", encoding="utf-8") as f:
                data = json.loads(f.read())
            lot = next((x for x in data if isinstance(x, dict)), None)
        except Exception as e:
            bot.send_message(m.chat.id, f"❌ Не удалось прочитать образец: <b>{e}</b>")
            return
        if not lot:
            bot.send_message(m.chat.id, "❌ В образце нет лотов.")
            return

        try:
            form = get_form(node_id)
        except Exception as e:
            bot.send_message(m.chat.id, f"❌ Не удалось получить форму раздела {node_id}: <b>{e}</b>")
            return

        desc_ru = str(lot.get("fields[desc][ru]", ""))
        desc_en = str(lot.get("fields[desc][en]", ""))
        title = str(lot.get("fields[summary][ru]", "тест"))

        def cut(text: str, limit: int) -> str:
            """Обрезает по границе строки, чтобы текст оставался читаемым."""
            if len(text) <= limit:
                return text
            part = text[:limit]
            if "\n" in part:
                part = part[:part.rfind("\n")]
            return part

        cases: list[tuple[str, dict[str, str]]] = []
        for limit in (200, 400, 600, 800, 1000, 1200, 1500):
            v = dict(form.fields)
            v.update({
                "fields[name][ru]": "", "fields[name][en]": "",
                "fields[summary][ru]": title, "fields[summary][en]": title,
                "fields[desc][ru]": cut(desc_ru, limit),
                "fields[desc][en]": cut(desc_en, limit),
                "fields[payment_msg][ru]": cut(str(lot.get("fields[payment_msg][ru]", "")), 300),
                "fields[payment_msg][en]": cut(str(lot.get("fields[payment_msg][en]", "")), 300),
                "price": str(lot.get("price") or "49"), "amount": "1", "active": "",
            })
            for key in ("fields[game]", "fields[type]", "fields[method]"):
                if lot.get(key):
                    v[key] = str(lot[key])
            cases.append((f"описание до {limit} символов", v))

        # проверка «дело в длине или в содержимом»: тот же объём обычным текстом
        filler = ("Проверка длины описания без эмодзи. " * 60)[:len(desc_ru)]
        v = dict(form.fields)
        v.update({
            "fields[name][ru]": "", "fields[name][en]": "",
            "fields[summary][ru]": title, "fields[summary][en]": title,
            "fields[desc][ru]": filler, "fields[desc][en]": filler,
            "fields[payment_msg][ru]": "тест", "fields[payment_msg][en]": "test",
            "price": str(lot.get("price") or "49"), "amount": "1", "active": "",
        })
        for key in ("fields[game]", "fields[type]", "fields[method]"):
            if lot.get(key):
                v[key] = str(lot[key])
        cases.append((f"текст без эмодзи, {len(filler)} символов", v))

        bot.send_message(m.chat.id, f"📏 Проверяю {len(cases)} вариантов длины описания. "
                                    f"Каждый тестовый лот удаляется сразу...")
        results: list[str] = []
        best = 0
        for label, fields in cases:
            fields = dict(fields)
            fields["offer_id"] = "0"
            before = lot_ids(cardinal, node_id)
            time.sleep(1)
            try:
                ok, offer_id, message = save_and_verify(cardinal, node_id, fields, title,
                                                        str(fields.get("price") or "49"), before)
            except Exception as e:
                ok, offer_id, message = False, None, str(e)
            if ok:
                results.append(f"✅ {label}")
                if offer_id:
                    delete_lot_raw(cardinal, form.fields, offer_id)
                else:
                    for new_id in sorted(lot_ids(cardinal, node_id) - before):
                        delete_lot_raw(cardinal, form.fields, new_id)
                if label.startswith("описание"):
                    best = max(best, int(label.split()[-2]))
            else:
                results.append(f"❌ {label}\n     {message}")

        text = ("📏 <b>Длина описания</b>\n\n" + "\n".join(results))
        if best:
            text += f"\n\n🎯 Максимальная рабочая длина: около <b>{best}</b> символов."
        else:
            text += "\n\n😕 Не прошёл даже 200-символьный вариант — причина не в длине."
        for i in range(0, len(text), 3500):
            bot.send_message(m.chat.id, text[i:i + 3500])

    def act_create_lots(m: Message):
        global RUNNING
        if RUNNING:
            bot.send_message(m.chat.id, "❌ Создание лотов уже идёт. Дождитесь окончания.")
            return
        result = bot.send_message(m.chat.id,
                                  "Отправьте JSON-файл со лотами (формат как у /create_lots).\n"
                                  "Плагин возьмёт свежую форму каждого раздела и подставит в неё "
                                  "только те поля, которые в этой форме есть.",
                                  reply_markup=skb.CLEAR_STATE_BTN())
        tg.set_state(m.chat.id, result.id, m.from_user.id, CBT_WAIT_FILE)

    def create_lots(m: Message):
        global RUNNING
        tg.clear_state(m.chat.id, m.from_user.id, True)

        if RUNNING:
            bot.send_message(m.chat.id, "❌ Создание лотов уже идёт.")
            return
        if not m.document or not m.document.file_name.endswith(".json"):
            bot.send_message(m.chat.id, "❌ Это не JSON-файл.")
            return

        RUNNING = True
        created = 0
        failed = 0
        unverified = 0
        inactive = 0
        removed_probe = 0
        try:
            if not download_file(tg, m):
                return

            with open("storage/cache/fresh_lots.json", "r", encoding="utf-8") as f:
                data = json.loads(f.read())

            if isinstance(data, dict):
                data = [data]
            if not isinstance(data, list) or not data:
                bot.send_message(m.chat.id, "❌ В файле должен быть непустой список лотов.")
                return

            bot.send_message(m.chat.id, f"Получено {len(data)} лот(-а/-ов).\n"
                                        f"Создаю по свежей форме раздела (1 лот/сек)...")

            forms: dict[str, OfferForm] = {}

            for index, lot in enumerate(data, start=1):
                if not isinstance(lot, dict):
                    bot.send_message(m.chat.id, f"❌ Элемент #{index} — не объект JSON. Пропускаю.")
                    failed += 1
                    continue

                node_id = str(lot.get("node_id") or "").strip()
                title = lot.get("fields[summary][ru]") or lot.get("fields[summary][en]") or f"#{index}"

                if not node_id.isdigit():
                    bot.send_message(m.chat.id, f"❌ [{index}] <code>{title}</code>: не указан node_id.")
                    failed += 1
                    continue

                try:
                    time.sleep(1)
                    if node_id not in forms:
                        forms[node_id] = get_form(node_id)

                    same = find_same_title(cardinal, node_id, title)
                    if same and is_probe_leftover(same):
                        # Тестовый лот от /fresh_probe с тем же заголовком — удаляем,
                        # он мешает создать настоящий.
                        try:
                            cardinal.account.delete_lot(int(same.id))
                            removed_probe += 1
                            logger.info(f"[FRESH LOTS] Удалил тестовый лот {same.id} с тем же заголовком.")
                            same = None
                        except Exception:
                            logger.warning(f"[FRESH LOTS] Не удалось удалить тестовый лот {same.id}.")
                    if same and str(lot.get("active", "")).strip().lower() in ("on", "1", "true", "да"):
                        raise RuntimeError(
                            f"в разделе уже есть лот с тем же заголовком (id <code>{same.id}</code>, "
                            f"{'активный' if same.active else 'неактивный'}). FunPay не разрешает дубли. "
                            f"Включите/удалите его вручную или измените заголовок."
                        )

                    fields, applied, skipped = forms[node_id].build(lot)
                    if skipped:
                        logger.warning(f"[FRESH LOTS] Поля, которые не перенеслись: {', '.join(skipped)}")

                    ok, offer_id, message, created_inactive, attempts = create_lot_with_fallback(
                        cardinal, node_id, fields, title, fields.get("price", "0"))

                    if not ok:
                        raise RuntimeError("\n".join(attempts))

                    created += 1
                    if created_inactive:
                        inactive += 1
                        logger.info(f"[FRESH LOTS] Лот создан НЕАКТИВНЫМ: {title} (id={offer_id}). "
                                    f"Попытки: {' | '.join(attempts)}")
                    else:
                        logger.info(f"[FRESH LOTS] Лот создан и подтверждён: {title} (id={offer_id}).")
                except Exception as e:
                    failed += 1
                    logger.error(f"[FRESH LOTS] Не удалось создать лот {title}.")
                    logger.debug("TRACEBACK", exc_info=True)
                    bot.send_message(m.chat.id,
                                     f"❌ [{index}] <code>{title}</code>\n"
                                     f"Раздел: <a href=\"https://funpay.com/lots/{node_id}/trade\">{node_id}</a>\n"
                                     f"Ошибка: <b>{e}</b>")

            summary = f"✅ Готово. Создано: <b>{created}</b>, с ошибкой: <b>{failed}</b>."
            if removed_probe:
                summary += f"\n🧹 Удалил тестовых лотов с теми же заголовками: <b>{removed_probe}</b>."
            if inactive:
                summary += (f"\n⚠️ Из них <b>{inactive}</b> созданы <b>неактивными</b> — FunPay не принял "
                            f"создание сразу активного лота. Включите их в разделе "
                            f"<a href=\"https://funpay.com/lots/402/trade\">402</a> "
                            f"(или напишите мне — сделаю команду включения).")
            if unverified:
                summary += (f"\n⚠️ Не удалось подтвердить в разделе: <b>{unverified}</b> "
                            f"(FunPay ответил успехом, но лота не видно). Проверьте: "
                            f"<code>/fresh_list 402</code>")
            hint = ""
            if failed:
                hint = ("\n\nПодсказка: <code>/fresh_check &lt;node_id&gt;</code> покажет поля раздела "
                        "и допустимые значения списков.")
            bot.send_message(m.chat.id, summary + hint)
        except Exception:
            logger.error("[FRESH LOTS] Не удалось выполнить создание лотов.")
            logger.debug("TRACEBACK", exc_info=True)
            bot.send_message(m.chat.id, "❌ Не удалось выполнить создание лотов. Подробности в логах.")
        finally:
            RUNNING = False

    cardinal.add_telegram_commands(UUID, [
        ("fresh_lots", "создает лоты по свежей форме раздела (обходит «Заполните все поля»)", True),
        ("fresh_check", "показать поля и допустимые значения раздела: /fresh_check 402", True),
        ("fresh_probe", "диагностика раздела перебором вариантов: /fresh_probe 402", True),
        ("fresh_cleanup", "удалить тестовые лоты после диагностики: /fresh_cleanup 402", True),
        ("fresh_list", "показать свои лоты раздела (включая неактивные): /fresh_list 402", True),
        ("fresh_bisect", "найти виноватое поле: /fresh_bisect 402", True),
        ("fresh_desc", "найти максимальную длину описания: /fresh_desc 402", True),
        ("fresh_raw", "полный разбор ответа FunPay на создание лота: /fresh_raw 402", True),
        ("fresh_find", "найти, в каких разделах у вас вообще есть лоты: /fresh_find", True),
        ("fresh_activate", "включить неактивные лоты раздела: /fresh_activate 402", True),
    ])

    tg.msg_handler(act_create_lots, commands=["fresh_lots"])
    tg.msg_handler(fresh_check, commands=["fresh_check"])
    tg.msg_handler(fresh_probe, commands=["fresh_probe"])
    tg.msg_handler(fresh_cleanup, commands=["fresh_cleanup"])
    tg.msg_handler(fresh_list, commands=["fresh_list"])
    tg.msg_handler(act_bisect, commands=["fresh_bisect"])
    tg.file_handler(CBT_BISECT, bisect)
    tg.msg_handler(fresh_desc, commands=["fresh_desc"])
    tg.msg_handler(fresh_raw, commands=["fresh_raw"])
    tg.msg_handler(fresh_find, commands=["fresh_find"])
    tg.msg_handler(fresh_activate, commands=["fresh_activate"])
    tg.file_handler(CBT_WAIT_FILE, create_lots)


BIND_TO_PRE_INIT = [init_commands]
BIND_TO_DELETE = None
