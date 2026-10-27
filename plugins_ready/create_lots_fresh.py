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
    /fresh_lots        → бот ждёт JSON-файл со лотами (формат как у /create_lots).
"""

from __future__ import annotations

import json
import time
from logging import getLogger
from typing import TYPE_CHECKING

from bs4 import BeautifulSoup

if TYPE_CHECKING:
    from cardinal import Cardinal

from telebot.types import Message
from tg_bot import static_keyboards as skb

NAME = "Create Lots Fresh"
VERSION = "2.0.0"
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
            if key not in fields:
                skipped.append(key)
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
    ])

    tg.msg_handler(act_create_lots, commands=["fresh_lots"])
    tg.msg_handler(fresh_check, commands=["fresh_check"])
    tg.file_handler(CBT_WAIT_FILE, create_lots)


BIND_TO_PRE_INIT = [init_commands]
BIND_TO_DELETE = None
