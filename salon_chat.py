import logging
import re
import secrets
from datetime import date, datetime, timedelta
from pathlib import Path
from threading import RLock
from time import monotonic
from typing import Callable

import psycopg
from psycopg.rows import dict_row

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, ValidationError


logger = logging.getLogger(__name__)
SESSION_COOKIE = "salon_chat_session"
SESSION_TTL = 30 * 60
MAX_SESSIONS = 1000


class ChatMessage(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


class ChatReply(BaseModel):
    reply: str


def normalize(text: str) -> str:
    text = text.casefold().replace("ё", "е")
    return " ".join(text.split()).strip(" .!?")


def service_forms(name: str) -> set[str]:
    name = normalize(name)
    forms = {name}
    if name.endswith("а"):
        forms.add(name[:-1] + "у")
    if name.endswith("ие"):
        forms.add(name[:-2] + "ия")
        forms.add(name[:-2] + "ию")
    return forms


def mentions_service(text: str, name: str) -> bool:
    return any(
        re.search(rf"(?<!\w){re.escape(form)}(?!\w)", text)
        is not None
        for form in service_forms(name)
    )


MONTHS = {"январ": 1, "феврал": 2, "март": 3, "апрел": 4,
          "ма": 5, "июн": 6, "июл": 7, "август": 8,
          "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12}


def extract_date(text):
    text = normalize(text)
    today = date.today()
    for word, days in (("послезавтра", 2), ("завтра", 1), ("сегодня", 0)):
        m = re.search(r"\b" + word + r"\b", text)
        if m:
            return today + timedelta(days=days), m.span()
    m = re.search(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)", text)
    if m:
        return date(*map(int, m.groups())), m.span()
    m = re.search(r"(?<![\d:])(\d{1,2})[./-](\d{1,2})(?:[./-](\d{4}|\d{2})(?!\d))?(?!\d)", text)
    if m:
        day, month, year = m.groups()
        # 13.00 is a time, not a calendar date. Other invalid dates raise ValueError.
        if int(month) == 0 or int(month) > 12:
            if re.search(r"\d{1,2}\.\d{2}", m.group()) and not year:
                return None, None
        year = int(year) if year else today.year
        if year < 100:
            year += 2000
        return date(year, int(month), int(day)), m.span()
    m = re.search(r"\b(\d{1,2})\s+(январ\w*|феврал\w*|март\w*|апрел\w*|мая|май|июн\w*|июл\w*|август\w*|сентябр\w*|октябр\w*|ноябр\w*|декабр\w*)(?:\s+(\d{4}))?\b", text)
    if m:
        day, word, year = m.groups()
        month = next(v for k, v in MONTHS.items() if word.startswith(k))
        return date(int(year) if year else today.year, month, int(day)), m.span()
    return None, None


def parse_chat_date(text: str) -> date | None:
    return extract_date(text)[0]


def extract_time(text, date_span=None):
    masked = text
    if date_span:
        masked = text[:date_span[0]] + " " * (date_span[1]-date_span[0]) + text[date_span[1]:]
    m = re.search(r"(?<!\d)(\d{1,2})[:.](\d{2})(?!\d)", masked)
    if not m:
        m = re.search(r"\b(?:в|на)\s+(\d{1,2})(?:\s+(\d{2}))?\b", masked)
    if not m and re.fullmatch(r"\d{1,2}:\d{2}", masked.strip()):
        m = re.search(r"(\d{1,2}):(\d{2})", masked)
    if not m:
        return None, None
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    if hour > 23 or minute > 59:
        raise ValueError("Некорректное время")
    return f"{hour:02d}:{minute:02d}", m.span()



def install_chat(
    app: FastAPI,
    project_dir: Path,
    get_services: Callable,
    get_masters_for_service: Callable,
    get_master_availability: Callable,
    create_appointment: Callable,
    appointment_model: type[BaseModel],
    database_url: str,
):
    """Локальный учебный чат: один процесс сервера, без авторизации."""
    page = project_dir / "chat.html"
    if not page.is_file():
        raise RuntimeError("Помести chat.html рядом с main.py")

    # В cookie только случайный идентификатор, не имя и не параметры записи.
    # Состояние диалога живёт в памяти одного процесса 30 минут бездействия.
    sessions = {}
    lock = RLock()

    @app.get("/chat", include_in_schema=False)
    def chat_page():
        return FileResponse(page, media_type="text/html")

    def session_for(request: Request, response: Response):
        now = monotonic()
        for key in list(sessions):
            if now - sessions[key]["touched"] >= SESSION_TTL:
                del sessions[key]

        token = request.cookies.get(SESSION_COOKIE)
        if token not in sessions:
            if len(sessions) >= MAX_SESSIONS:
                oldest = min(sessions, key=lambda key: sessions[key]["touched"])
                del sessions[oldest]
            token = secrets.token_urlsafe(32)
            sessions[token] = {"touched": now, "phase": "service", "owned_ids": set()}

        state = sessions[token]
        state["touched"] = now
        response.set_cookie(
            key=SESSION_COOKIE,
            value=token,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
            max_age=SESSION_TTL,
            path="/chat",
        )
        return state

    def reset_selection(state, service=None):
        touched = state["touched"]
        owned_ids = state.get("owned_ids", set())
        state.clear()
        state.update(
            touched=touched,
            owned_ids=owned_ids,
            phase="date" if service else "service",
        )
        if service is not None:
            state["service"] = service

    def show_slots(state, appointment_date):
        service = state["service"]
        reset_selection(state, service)
        state["date"] = appointment_date
        masters = jsonable_encoder(
            get_masters_for_service(service_id=service["id"])
        )
        if not masters:
            return ChatReply(reply="Для этой услуги пока нет подходящих мастеров.")

        lines = [
            f"Услуга: {service['name']}.",
            f"Дата: {appointment_date.strftime('%d.%m.%Y')}.",
        ]
        options = []
        for master in masters:
            availability = jsonable_encoder(
                get_master_availability(
                    master_id=master["id"],
                    service_id=service["id"],
                    appointment_date=appointment_date,
                    step_minutes=30,
                )
            )
            slots = availability["slots"]
            if not slots:
                lines.append(f"\n{master['name']} — свободного времени нет.")
            for slot in slots:
                # Поддерживаем формат времени API HH:MM:SS.
                start = slot["start_time"][:5]
                end = slot["end_time"][:5]
                options.append({
                    "master_id": master["id"],
                    "master_name": master["name"],
                    "start": start,
                    "end": end,
                })
                lines.append(
                    f"{len(options)}. {master['name']} "
                    f"(ID: {master['id']}) — {start}–{end}"
                )

        state["options"] = options
        if options:
            state["phase"] = "slot"
            lines.append(
                "\nВыберите время: напишите «вариант 1» "
                "или другой номер из списка.\n"
                "Это ещё не бронь. Можно отправить другую дату "
                "или выбрать другую услугу."
            )
        else:
            lines.append("\nНа эту дату свободного времени нет. Отправьте другую дату.")
        return ChatReply(reply="\n".join(lines))

    def confirmation(state):
        option = state["selected"]
        return ChatReply(reply=(
            "Проверьте запись:\n\n"
            f"Услуга: {state['service']['name']}.\n"
            f"Мастер: {option['master_name']}.\n"
            f"Дата: {state['date'].strftime('%d.%m.%Y')}.\n"
            f"Время: {option['start']}–{option['end']}.\n"
            f"Имя: {state['client_name']}.\n\n"
            "Запись ещё не создана.\n"
            "Чтобы создать её, напишите «Да». Для сброса — «Отмена»."
        ))

    def confirm_booking(state):
        option = state["selected"]
        # Сначала валидация той же Pydantic-моделью, что у POST /appointments.
        try:
            payload = appointment_model(
                master_id=option["master_id"],
                service_id=state["service"]["id"],
                appointment_date=state["date"],
                start_time=option["start"],
                client_name=state["client_name"],
            )
        except ValidationError:
            state["phase"] = "name"
            state.pop("client_name", None)
            return ChatReply(reply=(
                "Данные не прошли проверку модели записи. "
                "Проверьте ограничения AppointmentCreate. "
                "Можно повторно ввести имя или написать «Отмена»."
            ))

        # Блокируем автоматический повтор при неопределённом результате.
        state["phase"] = "uncertain"
        try:
            # Реальная функция API: её транзакция повторно проверяет время.
            result = create_appointment(payload=payload)
        except HTTPException as exc:
            if exc.status_code >= 500:
                logger.exception("Ошибка сервера при создании записи из чата")
                return uncertain_reply()
            # Для показанного обработчика 4xx означает отсутствие INSERT/commit.
            service = state["service"]
            reset_selection(state, service)
            return ChatReply(reply=(
                f"Запись не создана: {exc.detail}\n"
                "Отправьте дату ещё раз, чтобы получить актуальное свободное время."
            ))
        except Exception:
            logger.exception("Не удалось определить результат создания записи")
            return uncertain_reply()

        # Транзакция завершилась успешно. Дальше повторное «Да» не пишет в БД.
        # Даже ошибка форматирования ответа не должна разрешать повтор INSERT.
        state["phase"] = "done"
        state.pop("client_name", None)
        state.pop("options", None)
        try:
            saved = jsonable_encoder(result)
            # Владелец определяется только результатом серверного бронирования.
            # Никакие ID из сообщения клиента сюда не попадают.
            state["owned_ids"].add(int(saved["id"]))
            receipt = (
                f"Запись №{saved['id']} создана.\n"
                f"Услуга: {state['service']['name']}.\n"
                f"Мастер: {option['master_name']}.\n"
                f"Дата: {state['date'].strftime('%d.%m.%Y')}.\n"
                f"Время: {saved['start_time'][:5]}–{saved['end_time'][:5]}.\n"
                f"Имя: {saved['client_name']}.\n\n"
                "Для просмотра напишите «Мои записи». "
                "Для отмены — «Отмени мою запись». "
                "Для новой записи выберите услугу."
            )
        except Exception:
            logger.exception("Запись создана, но не удалось сформировать квитанцию")
            receipt = (
                "Запись создана, но подробности ответа прочитать не удалось. "
                "Проверьте список записей. Повторно подтверждать не нужно."
            )
        state["receipt"] = receipt
        return ChatReply(reply=receipt)

    def uncertain_reply():
        return ChatReply(reply=(
            "Не удалось надёжно определить результат сохранения. "
            "Не повторяйте бронирование: сначала проверьте список записей "
            "через существующий API и журнал сервера. "
            "Повторное «Да» в этом диалоге не создаст запись. "
            "После проверки напишите «Сброс»."
        ))

    def my_appointments(state):
        """Только будущие booked, созданные в этой серверной сессии."""
        owned_ids = sorted(state.get("owned_ids", set()))
        if not owned_ids:
            return []
        with psycopg.connect(database_url, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT a.id, a.master_id, a.service_id,
                           a.appointment_date, a.start_time, a.end_time,
                           a.client_name, a.status,
                           m.name AS master_name, s.name AS service_name
                    FROM appointments a
                    JOIN masters m ON m.id = a.master_id
                    JOIN services s ON s.id = a.service_id
                    WHERE a.id = ANY(%s)
                      AND a.status = 'booked'
                      AND (a.appointment_date + a.start_time) > %s
                    ORDER BY a.appointment_date, a.start_time, a.id
                """, (owned_ids, datetime.now()))
                return cur.fetchall()

    def appointment_description(item):
        return (
            f"{item['service_name']} — {item['master_name']}, "
            f"{item['appointment_date'].strftime('%d.%m.%Y')}, "
            f"{item['start_time'].strftime('%H:%M')}–"
            f"{item['end_time'].strftime('%H:%M')}"
        )

    def cancel_confirmation(state, item):
        # Здесь только подготовка подтверждения, UPDATE ещё нет.
        state['phase'] = 'cancel_confirm'
        state['cancel_id'] = item['id']
        state['cancel_snapshot'] = dict(item)
        return ChatReply(reply=(
            "Отменить эту запись?\n\n"
            f"{appointment_description(item)}.\n"
            f"Имя: {item['client_name']}.\n\n"
            "Пока ничего не отменено.\n"
            "Ответьте «Да» для отмены или «Нет», чтобы сохранить запись."
        ))

    def start_my_appointments(state, cancel=False):
        items = my_appointments(state)
        # Команда переключает сценарий и снимает старое подтверждение.
        reset_selection(state)
        if not items:
            return ChatReply(reply=(
                "В этом чате нет ваших будущих действующих записей.\n"
                "Я вижу только записи, созданные в текущей сессии этого браузера. "
                "Записи из другого браузера или до обновления чата сюда не привязаны. "
                "Если такая запись есть, обратитесь к администратору салона."
            ))
        if cancel and len(items) == 1:
            return cancel_confirmation(state, items[0])
        lines = ["Какую вашу запись отменить?" if cancel else "Ваши будущие записи:"]
        for index, item in enumerate(items, 1):
            lines.append(f"{index}. {appointment_description(item)}.")
        if cancel:
            state['phase'] = 'cancel_select'
            state['cancel_options'] = items
            lines.append(
                "\nНапишите название услуги, дату или номер варианта из этого списка. "
                "Номер записи из базы не нужен. Для выхода — «Нет»."
            )
        else:
            lines.append("\nДля отмены напишите «Отмени мою запись».")
        return ChatReply(reply="\n".join(lines))

    def choose_cancellation(text, state):
        options = state.get('cancel_options', [])
        choice = re.fullmatch(r"(?:вариант\s+)?(\d+)", text)
        if choice:
            index = int(choice.group(1)) - 1
            matches = options[index:index + 1] if 0 <= index < len(options) else []
        else:
            # Несколько признаков можно объединить: «стрижку 21.10.2026».
            matches = options
            criteria = False
            service_matches = [x for x in options if mentions_service(text, x['service_name'])]
            if service_matches:
                criteria = True
                ids = {x['id'] for x in service_matches}
                matches = [x for x in matches if x['id'] in ids]
            date_match = re.search(r"(?<!\d)(\d{4}-\d{2}-\d{2}|\d{2}\.\d{2}\.\d{4})(?!\d)", text)
            if date_match:
                criteria = True
                try:
                    chosen_date = parse_chat_date(date_match.group(1))
                except ValueError:
                    return ChatReply(reply="Такой даты нет. Пример: 21.10.2026.")
                matches = [x for x in matches if x['appointment_date'] == chosen_date]
            elif text in {'сегодня', 'завтра'}:
                criteria = True
                chosen_date = parse_chat_date(text)
                matches = [x for x in matches if x['appointment_date'] == chosen_date]
            time_match = re.search(r"(?<!\d)(\d{2}:\d{2})(?!\d)", text)
            if time_match:
                criteria = True
                matches = [x for x in matches if x['start_time'].strftime('%H:%M') == time_match.group(1)]
            if not criteria:
                matches = []
        if len(matches) > 1:
            return ChatReply(reply=(
                "Подходит несколько ваших записей. Уточните дату, время "
                "или номер варианта из показанного списка. Ничего не отменено."
            ))
        if not matches:
            return ChatReply(reply=(
                "Не нашёл такой вариант в вашем списке. Напишите название услуги, "
                "дату или номер варианта. Для обновления — «Отмени мою запись»."
            ))
        # Список мог устареть; подтверждаем только актуальные данные.
        fresh = next((x for x in my_appointments(state) if x['id'] == matches[0]['id']), None)
        if fresh is None:
            return start_my_appointments(state, cancel=True)
        return cancel_confirmation(state, fresh)

    def perform_cancellation(state):
        selected_id = state.get('cancel_id')
        if selected_id not in state.get('owned_ids', set()):
            reset_selection(state)
            return ChatReply(reply="Запись не связана с этой сессией. Напишите «Мои записи».")
        snapshot = state['cancel_snapshot']
        # При неясном исходе повторное «Да» не запускает UPDATE.
        state['phase'] = 'cancel_uncertain'
        changed = False
        already_cancelled = False
        fresh = None
        try:
            with psycopg.connect(database_url, row_factory=dict_row) as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT id, master_id, service_id, appointment_date,
                               start_time, end_time, client_name, status
                        FROM appointments
                        WHERE id = %s AND id = ANY(%s)
                        FOR UPDATE
                    """, (selected_id, sorted(state['owned_ids'])))
                    current = cur.fetchone()
                    if current is not None:
                        already_cancelled = current['status'] == 'cancelled'
                        same_details = all(
                            current[key] == snapshot[key]
                            for key in ('master_id', 'service_id', 'appointment_date',
                                        'start_time', 'end_time', 'client_name')
                        )
                        future = datetime.combine(current['appointment_date'], current['start_time']) > datetime.now()
                        if current['status'] == 'booked' and future:
                            if same_details:
                                cur.execute("""
                                    UPDATE appointments
                                    SET status = 'cancelled'
                                    WHERE id = %s AND id = ANY(%s)
                                      AND status = 'booked'
                                    RETURNING id, status
                                """, (selected_id, sorted(state['owned_ids'])))
                                changed = cur.fetchone() is not None
                            else:
                                fresh = dict(snapshot)
                                fresh.update(current)
        except Exception:
            logger.exception("Не удалось определить результат отмены из чата")
            return ChatReply(reply=(
                "Не удалось подтвердить результат отмены. Повторное «Да» ничего не изменит. "
                "Проверьте «Мои записи» или обратитесь к администратору."
            ))
        # Сюда попадаем только после успешного выхода из транзакции.
        if fresh is not None:
            updated = next((x for x in my_appointments(state) if x['id'] == selected_id), None)
            if updated is not None:
                reply = cancel_confirmation(state, updated)
                return ChatReply(reply="Данные записи изменились. Нужно новое подтверждение.\n\n" + reply.reply)
        if changed or already_cancelled:
            receipt = (
                "Запись отменена. Статус: cancelled.\n"
                "Для просмотра остальных напишите «Мои записи». "
                "Для новой записи напишите название услуги."
            )
            reset_selection(state)
            state.update(phase='cancel_done', receipt=receipt)
            return ChatReply(reply=receipt)
        reset_selection(state)
        return ChatReply(reply=(
            "Эту запись сейчас нельзя отменить: она отсутствует, уже началась "
            "или больше не является действующей. Напишите «Мои записи»."
        ))

    def build_reply(raw, text, state):
        if text in {"сброс", "отмена", "начать заново", "/reset"}:
            reset_selection(state)
            return ChatReply(reply=(
                "Текущий выбор сброшен. Уже созданные записи НЕ отменяются.\n"
                "Напишите «услуги» или название услуги."
            ))

        if state["phase"] == "uncertain":
            return uncertain_reply()

        if text in {"мои записи", "покажи мои записи", "какие у меня записи", "моя запись"}:
            return start_my_appointments(state)

        cancel_intent = bool(re.search(r"\b(?:отмени|отменить|отмените|отменила|отмети)\b", text))
        if cancel_intent and re.search(r"\bзапис\w*", text):
            # Даже если клиент дописал ID, он не используется для поиска.
            return start_my_appointments(state, cancel=True)

        if state["phase"] in {"cancel_select", "cancel_confirm"}:
            if text in {"нет", "не надо", "сохранить", "оставить"}:
                reset_selection(state)
                return ChatReply(reply="Отмена не выполнена. Ваши записи сохранены.")
            if state["phase"] == "cancel_select":
                return choose_cancellation(text, state)
            if text in {"да", "подтверждаю"}:
                return perform_cancellation(state)
            return ChatReply(reply="Ответьте «Да», чтобы отменить выбранную запись, или «Нет», чтобы сохранить.")

        if state["phase"] == "cancel_uncertain":
            return ChatReply(reply=(
                "Результат отмены пока не подтверждён. Повторное «Да» ничего не изменит. "
                "Напишите «Мои записи» для проверки."
            ))
        if state["phase"] == "cancel_done" and text in {"да", "подтверждаю"}:
            return ChatReply(reply=state["receipt"])

        if text in {"привет", "здравствуйте", "начать", "/start"}:
            return ChatReply(reply=(
                "Здравствуйте! Помогу записаться в салон.\n"
                "Выберите услугу, дату, вариант времени и введите имя. "
                "Запись создаётся только после вопроса о подтверждении "
                "и вашего отдельного ответа «Да».\n"
                "Напишите «услуги», «Мои записи» или «Отмени мою запись». Для сброса — «Отмена»."
            ))

        if text in {"да", "подтверждаю"}:
            if state["phase"] == "confirm":
                return confirm_booking(state)
            if state["phase"] == "done":
                return ChatReply(reply=state["receipt"])
            return ChatReply(reply=(
                "Пока нечего подтверждать. Сначала выберите услугу, "
                "дату, время и введите имя."
            ))

        services = jsonable_encoder(get_services())
        if not services:
            return ChatReply(reply="В базе пока нет услуг.")

        if text in {"услуги", "список услуг", "какие есть услуги", "покажи услуги"}:
            lines = ["Услуги из базы салона:"]
            for service in services:
                lines.append(
                    f"• {service['name']} — {service['duration_minutes']} мин.; "
                    f"цена: {service['price']} (как в базе); ID: {service['id']}"
                )
            lines.append("\nНапишите название услуги или «услуга ID».")
            return ChatReply(reply="\n".join(lines))

        # Single booking parser at every booking step: gather fields first,
        # then ask only for missing/ambiguous fields. Cancellation stays separate.
        try:
            chosen_date, date_span = extract_date(text)
            wanted_time, time_span = extract_time(text, date_span)
        except ValueError:
            return ChatReply(reply="Проверьте дату и время: такой даты или времени нет. Например: «20 октября, 13:00».")
        if chosen_date and chosen_date < date.today():
            return ChatReply(reply="Эта дата уже прошла. Укажите сегодня или будущую дату.")

        id_match = re.fullmatch(r"услуга\s+(\d+)", text)
        matches = ([s for s in services if s['id'] == int(id_match.group(1))]
                   if id_match else [s for s in services if mentions_service(text, s['name'])])
        if len(matches) > 1:
            return ChatReply(reply="Уточните услугу: " + ", ".join(s['name'] for s in matches))
        if id_match and not matches:
            return ChatReply(reply="Такой услуги нет. Напишите «Услуги».")
        incoming_service = matches[0] if matches else None
        selected_service = incoming_service or state.get('service')
        masters = jsonable_encoder(get_masters_for_service(service_id=selected_service['id'])) if selected_service else []
        named_masters = [m for m in masters if re.search(r"(?<!\w)" + re.escape(normalize(m['name'])) + r"(?!\w)", text)]
        if len(named_masters) > 1:
            return ChatReply(reply="Указано несколько мастеров. Напишите имя одного мастера.")
        incoming_master = named_masters[0] if named_masters else None

        # Extract explicit client name or an unambiguous comma-separated remainder.
        name_match = re.search(r"(?:меня зовут|клиент(?:ка)?|имя)\s*[:—-]?\s*([а-яёa-z][а-яёa-z -]{1,99})", raw, re.I)
        client_name = name_match.group(1).strip(' .,') if name_match else None
        if not client_name and (',' in raw or incoming_service):
            remainder = text
            spans = [x for x in (date_span, time_span) if x]
            for left, right in sorted(spans, reverse=True):
                remainder = remainder[:left] + ' ' * (right-left) + remainder[right:]
            for item in matches + named_masters:
                for form in service_forms(item['name']):
                    remainder = re.sub(r'(?<!\w)' + re.escape(form) + r'(?!\w)', ' ', remainder)
            remainder = re.sub(r'\b(?:хочу|записаться|запишите|пожалуйста|на|к|мастер|лучше|давайте|в|мне)\b', ' ', remainder)
            remainder = ' '.join(remainder.strip(' ,.;:-').split())
            if re.fullmatch(r'[а-яёa-z][а-яёa-z -]{1,99}', remainder, re.I):
                client_name = remainder.title()
        structured = bool(incoming_service or chosen_date or wanted_time or incoming_master)
        if state['phase'] == 'name' and not structured and not client_name:
            candidate = raw.strip()
            if re.fullmatch(r"[а-яёa-z][а-яёa-z '-]{1,99}", candidate, re.I):
                client_name = candidate
            else:
                return ChatReply(reply="Как зовут клиента? Например: «Ольга».")

        choice = re.fullmatch(r'(?:вариант\s+)?(\d+)', text)
        if choice and not structured and not client_name:
            options = state.get('options', [])
            if state['phase'] not in {'slot', 'confirm', 'name'} or not options:
                return ChatReply(reply="Сначала выберите услугу и дату — покажу свободное время.")
            index = int(choice.group(1)) - 1
            if not 0 <= index < len(options):
                return ChatReply(reply="Такого варианта нет. Выберите из последнего списка.")
            state['selected'] = options[index]
            if state.get('client_name'):
                state['phase'] = 'confirm'
                return confirmation(state)
            state['phase'] = 'name'
            return ChatReply(reply=f"Выбрано: {options[index]['master_name']}, {options[index]['start']}. Как зовут клиента?")

        if not structured and not client_name:
            if state['phase'] == 'confirm':
                return confirmation(state)
            if state['phase'] == 'done':
                return ChatReply(reply=state['receipt'])
            return ChatReply(reply="Можно написать «Анна, 13:00», «завтра в 16:00» или услугу и пожелания одной фразой. Если имя мастера не найдено, укажите его как в списке. Для сброса — «Отмена».")

        if state['phase'] in {'done', 'cancel_done'}:
            reset_selection(state)
        if incoming_service:
            if state.get('service', {}).get('id') != incoming_service['id']:
                state.pop('wanted_master', None)
                state.pop('selected', None)
                state.pop('options', None)
                state.pop('all_options', None)
            state['service'] = incoming_service
        if chosen_date:
            state['date'] = chosen_date
        if wanted_time:
            state['wanted_time'] = wanted_time
        if incoming_master:
            state['wanted_master'] = incoming_master['id']
        if client_name:
            state['client_name'] = client_name
        if 'service' not in state:
            state['phase'] = 'service'
            return ChatReply(reply="Пожелания сохранил. На какую услугу записать? Напишите «Услуги», чтобы посмотреть список.")
        if 'date' not in state:
            state['phase'] = 'date'
            return ChatReply(reply=f"Услуга: {state['service']['name']}. На какую дату? Можно «20.10», «20 октября», «завтра».")

        # Refresh slots without discarding accumulated client/time/master fields.
        if structured or not state.get('selected'):
            saved = {k: state[k] for k in ('client_name', 'wanted_time', 'wanted_master') if k in state}
            target_date = state['date']
            show_slots(state, target_date)
            state.update(saved)
            all_options = state.get('options', [])
            state['all_options'] = all_options
            options = [o for o in all_options
                       if (not state.get('wanted_time') or o['start'] == state['wanted_time'])
                       and (not state.get('wanted_master') or o['master_id'] == state['wanted_master'])]
            state['options'] = options
            state.pop('selected', None)
            state['phase'] = 'slot'
            if not options:
                time_hint = state.get('wanted_time', 'это время')
                return ChatReply(reply=f"На {time_hint} с выбранными пожеланиями свободного варианта нет. Укажите другое время или имя другого мастера. Услуга, дата и имя клиента сохранены. Для полного сброса — «Отмена».")
            if len(options) > 1:
                return ChatReply(reply="Выберите мастера и время — можно именем, временем или номером:\n" + '\n'.join(f"{i}. {o['master_name']} — {o['start']}–{o['end']}" for i, o in enumerate(options, 1)))
            state['selected'] = options[0]
        if not state.get('client_name'):
            state['phase'] = 'name'
            option = state['selected']
            return ChatReply(reply=f"Выбрано: {option['master_name']}, {state['date'].strftime('%d.%m.%Y')}, {option['start']}–{option['end']}.\nКак зовут клиента? Запись ещё не создана.")
        state['phase'] = 'confirm'
        return confirmation(state)

    @app.post("/chat/message", response_model=ChatReply)
    def chat_message(payload: ChatMessage, request: Request, response: Response):
        text = normalize(payload.message)
        if not text:
            raise HTTPException(status_code=422, detail="Сообщение не должно быть пустым")
        # Включая создание записи: два параллельных «Да» не выполнят два INSERT.
        # Для учебного локального приложения сериализуем все сообщения.
        with lock:
            state = session_for(request, response)
            try:
                return build_reply(payload.message, text, state)
            except HTTPException as exc:
                if exc.status_code < 500:
                    return ChatReply(reply=f"Не удалось выполнить запрос: {exc.detail}")
                logger.exception("Ошибка API при обработке сообщения чата")
                return ChatReply(reply="Не удалось получить данные салона. Проверьте журнал сервера.")
            except Exception:
                logger.exception("Ошибка при обработке сообщения чата")
                return ChatReply(reply="Ошибка обработки сообщения. Проверьте журнал сервера и подключение к базе.")
