"""
Плагин для FunPay Cardinal: создание лотов по JSON «как сайт».

Зачем нужен:
    Штатный плагин Lots Copy (/create_lots) берёт JSON и прогоняет его через
    FunPayAPI.types.LotFields.renew_fields(). Тот принудительно:
      * добавляет "fields[images]" (пустым),
      * переводит цену в float ("49" -> "49.0"),
      * подставляет свой набор ключей.
    В разделах вроде 402 («Прочие игры Roblox» с полями game/type/method) из-за этого
    FunPay отвечает: «Заполните все поля.» и лот не создаётся.

    Этот плагин делает иначе и ровно так, как это делает сам сайт:
      1. GET lots/offerEdit?node=<node_id> — забирает СВЕЖУЮ форму раздела
         (все input/select/textarea, включая скрытые, и актуальные csrf_token /
         form_created_at);
      2. подменяет в этой форме только то, что нужно: тексты, цену, наличие, флаги
         и поля раздела из вашего JSON;
      3. POST lots/offerSave — отправляет форму как есть, без пересборки.

    Если FunPay всё же откажет, плагин покажет ТОЧНЫЙ текст ошибки от FunPay.

Установка:
    1. Меню бота → Плагины → ➕ Добавить плагин → отправить этот файл (.py).
    2. /restart — чтобы плагин загрузился.

Использование:
    /fresh_check 402  → показать, какие поля есть в форме раздела и какие обязательны
                        (без запуска чего-либо на хосте — всё из Telegram).

    /fresh_lots  → бот ждёт JSON-файл (список лотов, формат как у /create_lots).

    Каждый лот создаётся ~1 шт/сек. В конце — отчёт: сколько создано, где ошибки.
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
VERSION = "1.0.0"
DESCRIPTION = ("Создает лоты из JSON, используя свежую форму раздела (без пересборки полей). "
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


def parse_offer_form(html: str) -> dict[str, str]:
    """
    Разбирает форму /lots/offerEdit ровно так же, как это делает FunPayAPI,
    но без дополнительных запросов и без приведения типов.

    :param html: HTML страницы создания/редактирования лота.

    :return: словарь со всеми полями формы.

    :raises RuntimeError: если FunPay вернул ошибку или формы нет на странице.
    """
    bs = BeautifulSoup(html, "lxml")

    error = bs.find("p", class_="lead")
    if error is not None:
        raise RuntimeError(f"FunPay: {error.text.strip()}")

    form = bs.find("form", class_="form-offer-editor")
    if form is None:
        raise RuntimeError("на странице нет формы лота (нет доступа к разделу или неверный node_id)")

    fields: dict[str, str] = {}

    for inp in form.find_all("input"):
        name = inp.get("name")
        if name:
            fields[name] = inp.get("value") or ""

    for ta in form.find_all("textarea"):
        name = ta.get("name")
        if name:
            fields[name] = ta.text or ""

    for sel in form.find_all("select"):
        name = sel.get("name")
        if not name:
            continue
        parent = sel.find_parent(class_="form-group")
        if parent and "hidden" in parent.get("class", []):
            continue
        option = sel.find("option", selected=True) or sel.find("option")
        fields[name] = option.get("value", "") if option is not None else ""

    for cb in form.find_all("input", {"type": "checkbox"}, checked=True):
        if cb.get("name"):
            fields[cb["name"]] = "on"

    return fields


def describe_form(html: str) -> list[str]:
    """
    Возвращает человекочитаемое описание полей формы раздела: имя, обязательность,
    подпись и допустимые значения для select.

    :param html: HTML формы /lots/offerEdit.

    :return: список строк для отправки в Telegram.
    """
    bs = BeautifulSoup(html, "lxml")
    form = bs.find("form", class_="form-offer-editor")
    if form is None:
        raise RuntimeError("на странице нет формы лота (нет доступа к разделу или неверный node_id)")

    lines: list[str] = []
    labels: dict[str, tuple[str, bool]] = {}
    for group in form.find_all("div", class_="form-group"):
        label = group.find("label")
        control = group.find(["input", "select", "textarea"])
        if not label or not control or not control.get("name"):
            continue
        required = ("required" in group.get("class", [])
                    or bool(control.get("required"))
                    or bool(label.find("span", class_="text-danger")))
        labels[control["name"]] = (label.get_text(strip=True).rstrip("*").strip(), required)

    for sel in form.find_all("select"):
        name = sel.get("name")
        if not name:
            continue
        parent = sel.find_parent(class_="form-group")
        if parent and "hidden" in parent.get("class", []):
            continue
        label, required = labels.get(name, ("", False))
        options = [(o.get("value", ""), o.get_text(strip=True)) for o in sel.find_all("option")]
        current = sel.find("option", selected=True)
        lines.append(f"{'🔴' if required else '⚪'} <code>{name}</code> — {label}")
        lines.append(f"     сейчас: {current.get('value', '') if current else ''!r}")
        lines.append("     значения: " + ", ".join(f"{v} = {t}" for v, t in options[:25]))

    for inp in form.find_all("input"):
        name = inp.get("name")
        if not name or inp.get("type") == "hidden":
            continue
        label, required = labels.get(name, ("", False))
        if name.startswith("fields[") or name in ("price", "amount"):
            lines.append(f"{'🔴' if required else '⚪'} <code>{name}</code> — {label} "
                         f"(сейчас: {inp.get('value') or ''!r})")

    for ta in form.find_all("textarea"):
        name = ta.get("name")
        if not name:
            continue
        label, required = labels.get(name, ("", False))
        lines.append(f"{'🔴' if required else '⚪'} <code>{name}</code> — {label} "
                     f"(сейчас: {len(ta.text or '')} символов)")

    hidden = [inp["name"] for inp in form.find_all("input", {"type": "hidden"}) if inp.get("name")]
    lines.append("\n🫥 скрытые поля (их плагин отправляет как есть): " + ", ".join(hidden))
    return lines


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
    """
    Скачивает присланный файл в storage/cache/.
    """
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

    def create_lot_fresh(lot: dict, node_id: str) -> dict:
        """
        Создает один лот: свежая форма раздела + подстановка значений из JSON.
        """
        response = cardinal.account.method("get", f"lots/offerEdit?node={node_id}", {}, {},
                                           raise_not_200=True)
        fields = parse_offer_form(response.content.decode())

        for key, value in lot.items():
            if key in ("csrf_token", "offer_id"):
                continue
            fields[key] = value

        # Активность: плагин умеет только два состояния.
        fields["active"] = "on" if str(lot.get("active", "")).strip() == "on" else ""

        # Наличие: пустая строка = не отправляем значение, FunPay посчитает сам.
        amount = lot.get("amount")
        if amount in (None, ""):
            fields.pop("amount", None)
        else:
            fields["amount"] = str(amount)

        return save_offer(cardinal, fields)

    def fresh_check(m: Message):
        args = (m.text or "").split()
        node_id = args[1].strip() if len(args) > 1 else ""
        if not node_id.isdigit():
            bot.send_message(m.chat.id, "Использование: <code>/fresh_check 402</code>")
            return
        try:
            response = cardinal.account.method("get", f"lots/offerEdit?node={node_id}", {}, {},
                                               raise_not_200=True)
            lines = describe_form(response.content.decode())
        except Exception as e:
            bot.send_message(m.chat.id, f"❌ Не удалось получить форму раздела {node_id}: <b>{e}</b>")
            logger.debug("TRACEBACK", exc_info=True)
            return

        text = (f"Форма раздела <a href=\"https://funpay.com/lots/{node_id}/trade\">{node_id}</a>\n"
                f"🔴 — обязательное, ⚪ — необязательное\n\n" + "\n".join(lines))
        for i in range(0, len(text), 3500):
            bot.send_message(m.chat.id, text[i:i + 3500])

    def act_create_lots(m: Message):
        global RUNNING
        if RUNNING:
            bot.send_message(m.chat.id, "❌ Создание лотов уже идёт. Дождитесь окончания.")
            return
        result = bot.send_message(m.chat.id,
                                  "Отправьте JSON-файл со лотами (формат как у /create_lots).\n"
                                  "Плагин возьмёт свежую форму каждого раздела и подставит значения "
                                  "из файла — так же, как это делает сайт.",
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
                RUNNING = False
                return

            with open("storage/cache/fresh_lots.json", "r", encoding="utf-8") as f:
                data = json.loads(f.read())

            if isinstance(data, dict):
                data = [data]
            if not isinstance(data, list) or not data:
                bot.send_message(m.chat.id, "❌ В файле должен быть непустой список лотов.")
                RUNNING = False
                return

            bot.send_message(m.chat.id, f"Получено {len(data)} лот(-а/-ов).\n"
                                        f"Создаю по свежей форме раздела (1 лот/сек)...")

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
                    create_lot_fresh(lot, node_id)
                    created += 1
                    logger.info(f"[FRESH LOTS] Создал лот {title} в разделе {node_id}.")
                except Exception as e:
                    failed += 1
                    logger.error(f"[FRESH LOTS] Не удалось создать лот {title}.")
                    logger.debug("TRACEBACK", exc_info=True)
                    bot.send_message(m.chat.id,
                                     f"❌ [{index}] <code>{title}</code>\n"
                                     f"Раздел: <a href=\"https://funpay.com/lots/{node_id}/trade\">{node_id}</a>\n"
                                     f"Ошибка FunPay: <b>{e}</b>")

            bot.send_message(m.chat.id, f"✅ Готово. Создано: <b>{created}</b>, с ошибкой: <b>{failed}</b>.")
        except Exception:
            logger.error("[FRESH LOTS] Не удалось выполнить создание лотов.")
            logger.debug("TRACEBACK", exc_info=True)
            bot.send_message(m.chat.id, "❌ Не удалось выполнить создание лотов. Подробности в логах.")
        finally:
            RUNNING = False

    cardinal.add_telegram_commands(UUID, [
        ("fresh_lots", "создает лоты по свежей форме раздела (обходит «Заполните все поля»)", True),
        ("fresh_check", "показать поля и обязательность полей раздела: /fresh_check 402", True),
    ])

    tg.msg_handler(act_create_lots, commands=["fresh_lots"])
    tg.msg_handler(fresh_check, commands=["fresh_check"])
    tg.file_handler(CBT_WAIT_FILE, create_lots)


BIND_TO_PRE_INIT = [init_commands]
BIND_TO_DELETE = None
