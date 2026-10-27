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
    /fresh_cleanup 402 → удалить тестовые лоты, оставшиеся после /fresh_probe.
    /fresh_probe 402   → диагностика: создаёт по очереди несколько НЕАКТИВНЫХ лотов,
                         каждый со своим набором полей, и показывает, какой вариант
                         FunPay принимает. Нужен, когда непонятно, из-за какого поля
                         приходит «Заполните все поля».
    /fresh_lots        → бот ждёт JSON-файл со лотами (формат как у /create_lots).
"""

from __future__ import annotations

import json
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
VERSION = "2.2.0"
DESCRIPTION = ("Создает лоты из JSON по свежей форме раздела: отправляет только те поля, "
               "которые есть в форме, и подбирает значения списков. "
               "Решает ошибку «Заполните все поля» в разделах с доп. полями.")
CREDITS = "для FunPay Cardinal"
UUID = "b7f3c1a2-6f4d-4e2b-9a11-7d5c0e8f4b31"
SETTINGS_PAGE = False

logger = getLogger("FPC.create_lots_fresh")
RUNNING = False

CBT_WAIT_FILE = "create_lots_fresh.wait_file"

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


def save_offer(cardinal: Cardinal, fields: dict[str, str]) -> dict:
    """
    Отправляет форму на FunPay без пересборки полей (как это делает сайт).

    :param cardinal: объект кардинала.
    :param fields: поля формы.

    :return: распарсенный JSON-ответ FunPay.

    :raises RuntimeError: если FunPay вернул ошибку сохранения.
    """
    fields = dict(fields)
    fields["offer_id"] = "0"

    response = cardinal.account.method("post", "lots/offerSave", SAVE_HEADERS, fields, raise_not_200=True)

    try:
        payload = response.json()
    except Exception:
        payload = {}

    error = payload.get("error")
    if not error and payload.get("errors"):
        errors = payload["errors"]
        if isinstance(errors, list):
            error = "; ".join(f"{name}: {text}" for name, text in errors)
        else:
            error = str(errors)
    if error:
        raise RuntimeError(error)
    return payload


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
            try:
                time.sleep(1)
                payload = save_offer(cardinal, fields)
                offer_id = extract_offer_id(payload)
                results.append(f"✅ <b>{label}</b> — создан")
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
                    fields, applied, skipped = forms[node_id].build(lot)
                    save_offer(cardinal, fields)
                    created += 1
                    logger.info(f"[FRESH LOTS] Создал лот {title} в разделе {node_id}.")
                    if skipped:
                        logger.warning(f"[FRESH LOTS] В форме раздела {node_id} нет полей: {', '.join(skipped)}")
                except Exception as e:
                    failed += 1
                    logger.error(f"[FRESH LOTS] Не удалось создать лот {title}.")
                    logger.debug("TRACEBACK", exc_info=True)
                    bot.send_message(m.chat.id,
                                     f"❌ [{index}] <code>{title}</code>\n"
                                     f"Раздел: <a href=\"https://funpay.com/lots/{node_id}/trade\">{node_id}</a>\n"
                                     f"Ошибка: <b>{e}</b>")

            hint = ""
            if failed:
                hint = ("\n\nПодсказка: <code>/fresh_check &lt;node_id&gt;</code> покажет поля раздела "
                        "и допустимые значения списков.")
            bot.send_message(m.chat.id, f"✅ Готово. Создано: <b>{created}</b>, с ошибкой: <b>{failed}</b>." + hint)
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
    ])

    tg.msg_handler(act_create_lots, commands=["fresh_lots"])
    tg.msg_handler(fresh_check, commands=["fresh_check"])
    tg.msg_handler(fresh_probe, commands=["fresh_probe"])
    tg.msg_handler(fresh_cleanup, commands=["fresh_cleanup"])
    tg.file_handler(CBT_WAIT_FILE, create_lots)


BIND_TO_PRE_INIT = [init_commands]
BIND_TO_DELETE = None
