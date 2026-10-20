import os
from decimal import Decimal
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from datetime import date, datetime, time, timedelta
from fastapi import FastAPI, HTTPException, Query
from psycopg.rows import dict_row
from pydantic import BaseModel, Field
from psycopg.errors import UniqueViolation


# Читаем настройки из .env рядом с этим файлом.
project_dir = Path(__file__).resolve().parent
load_dotenv(project_dir / ".env")

database_url = os.getenv("DATABASE_URL")

if not database_url:
    raise RuntimeError("Не найдена DATABASE_URL. Проверь файл .env.")


app = FastAPI(
    title="API салона",
    description="Услуги и мастера салона.",
    version="0.1.0",
)


class Service(BaseModel):
    id: int
    name: str
    duration_minutes: int
    price: Decimal


@app.get("/")
def home():
    return {
        "message": "API салона работает",
        "services": "/services",
        "documentation": "/docs",
    }


@app.get("/services", response_model=list[Service])
def get_services():
    with psycopg.connect(
        database_url,
        row_factory=dict_row,
    ) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, name, duration_minutes, price
                FROM services
                ORDER BY name
            """)

            services = cur.fetchall()

    return services
class Master(BaseModel):
    id: int
    name: str


@app.get(
    "/services/{service_id}/masters",
    response_model=list[Master],
)
def get_masters_for_service(service_id: int):
    with psycopg.connect(
        database_url,
        row_factory=dict_row,
    ) as conn:
        with conn.cursor() as cur:

            # Проверяем, существует ли выбранная услуга.
            cur.execute("""
                SELECT id
                FROM services
                WHERE id = %s
            """, (service_id,))

            service = cur.fetchone()

            if service is None:
                raise HTTPException(
                    status_code=404,
                    detail="Услуга не найдена",
                )

            # Получаем мастеров, выполняющих эту услугу.
            cur.execute("""
                SELECT m.id, m.name
                FROM masters AS m
                JOIN master_services AS ms
                    ON ms.master_id = m.id
                WHERE ms.service_id = %s
                ORDER BY m.name
            """, (service_id,))

            masters = cur.fetchall()

    return masters
class ScheduleAppointment(BaseModel):
    id: int
    start_time: time
    end_time: time
    service_name: str
    client_name: str
    status: str


class WorkingHours(BaseModel):
    start_time: time
    end_time: time


class MasterSchedule(BaseModel):
    master_id: int
    master_name: str
    appointment_date: date
    working_hours: WorkingHours | None
    appointments: list[ScheduleAppointment]
@app.get(
    "/masters/{master_id}/schedule",
    response_model=MasterSchedule,
)
def get_master_schedule(
    master_id: int,
    appointment_date: date = Query(
        ...,
        description="Дата в формате YYYY-MM-DD",
    ),
):
    with psycopg.connect(
        database_url,
        row_factory=dict_row,
    ) as conn:
        with conn.cursor() as cur:

            # Проверяем существование мастера.
            cur.execute("""
                SELECT id, name
                FROM masters
                WHERE id = %s
            """, (master_id,))

            master = cur.fetchone()

            if master is None:
                raise HTTPException(
                    status_code=404,
                    detail="Мастер не найден",
                )

            # Получаем рабочие часы.
            #
            # ISODOW:
            # понедельник = 1,
            # вторник = 2,
            # ...
            # воскресенье = 7.
            cur.execute("""
                SELECT start_time, end_time
                FROM working_hours
                WHERE master_id = %s
                  AND weekday = EXTRACT(
                      ISODOW FROM %s::date
                  )::integer
            """, (master_id, appointment_date))

            working_hours_row = cur.fetchone()

            # Получаем все незакрытые записи мастера на дату.
            cur.execute("""
                SELECT
                    a.id,
                    a.start_time,
                    a.end_time,
                    s.name AS service_name,
                    a.client_name,
                    a.status
                FROM appointments AS a
                JOIN services AS s
                    ON s.id = a.service_id
                WHERE a.master_id = %s
                  AND a.appointment_date = %s
                  AND a.status <> 'cancelled'
                ORDER BY a.start_time
            """, (master_id, appointment_date))

            appointments = cur.fetchall()

    working_hours = None

    if working_hours_row is not None:
        working_hours = WorkingHours(
            start_time=working_hours_row["start_time"],
            end_time=working_hours_row["end_time"],
        )

    return MasterSchedule(
        master_id=master["id"],
        master_name=master["name"],
        appointment_date=appointment_date,
        working_hours=working_hours,
        appointments=appointments,
    )
class AvailableSlot(BaseModel):
    start_time: time
    end_time: time


class AvailabilityResponse(BaseModel):
    master_id: int
    master_name: str
    service_id: int
    service_name: str
    appointment_date: date
    duration_minutes: int
    step_minutes: int
    slots: list[AvailableSlot]


@app.get(
    "/masters/{master_id}/availability",
    response_model=AvailabilityResponse,
)
def get_master_availability(
    master_id: int,
    service_id: int = Query(
        ...,
        description="Идентификатор услуги",
        gt=0,
    ),
    appointment_date: date = Query(
        ...,
        description="Дата в формате YYYY-MM-DD",
    ),
    step_minutes: int = Query(
        30,
        description="Шаг между предлагаемыми вариантами начала",
        ge=5,
        le=120,
    ),
):
    # Используем уже готовую функцию получения расписания.
    # Она также проверяет, существует ли мастер.
    schedule = get_master_schedule(
        master_id=master_id,
        appointment_date=appointment_date,
    )

    with psycopg.connect(
        database_url,
        row_factory=dict_row,
    ) as conn:
        with conn.cursor() as cur:

            # Получаем услугу и её длительность.
            cur.execute("""
                SELECT id, name, duration_minutes
                FROM services
                WHERE id = %s
            """, (service_id,))

            service = cur.fetchone()

            if service is None:
                raise HTTPException(
                    status_code=404,
                    detail="Услуга не найдена",
                )

            # Проверяем, выполняет ли мастер эту услугу.
            cur.execute("""
                SELECT 1
                FROM master_services
                WHERE master_id = %s
                  AND service_id = %s
            """, (master_id, service_id))

            if cur.fetchone() is None:
                raise HTTPException(
                    status_code=400,
                    detail="Мастер не выполняет выбранную услугу",
                )

    duration_minutes = service["duration_minutes"]

    if duration_minutes <= 0:
        raise HTTPException(
            status_code=500,
            detail="У услуги некорректная длительность",
        )

    slots = []

    if schedule.working_hours is not None:
        work_start = datetime.combine(
            appointment_date,
            schedule.working_hours.start_time,
        )
        work_end = datetime.combine(
            appointment_date,
            schedule.working_hours.end_time,
        )

        duration = timedelta(minutes=duration_minutes)
        step = timedelta(minutes=step_minutes)

        # Превращаем занятые интервалы в пары datetime.
        busy_intervals = [
            (
                datetime.combine(
                    appointment_date,
                    appointment.start_time,
                ),
                datetime.combine(
                    appointment_date,
                    appointment.end_time,
                ),
            )
            for appointment in schedule.appointments
        ]
        candidate_start = work_start

        # Пока используем местное время сервера,
        # как при создании и переносе записи.
        now = datetime.now()
        # Услуга должна полностью помещаться в рабочий день.
        while candidate_start + duration <= work_end:
            candidate_end = candidate_start + duration

            overlaps = any(
                candidate_start < busy_end
                and candidate_end > busy_start
                for busy_start, busy_end in busy_intervals
            )

            if candidate_start > now and not overlaps:
                slots.append(
                    AvailableSlot(
                        start_time=candidate_start.time(),
                        end_time=candidate_end.time(),
                    )
                )

            candidate_start += step

    return AvailabilityResponse(
        master_id=schedule.master_id,
        master_name=schedule.master_name,
        service_id=service["id"],
        service_name=service["name"],
        appointment_date=appointment_date,
        duration_minutes=duration_minutes,
        step_minutes=step_minutes,
        slots=slots,
    )
class AppointmentCreate(BaseModel):
    master_id: int = Field(gt=0)
    service_id: int = Field(gt=0)
    appointment_date: date
    start_time: time
    client_name: str = Field(min_length=1, max_length=100)


class AppointmentResponse(BaseModel):
    id: int
    master_id: int
    service_id: int
    appointment_date: date
    start_time: time
    end_time: time
    client_name: str
    status: str


@app.post(
    "/appointments",
    response_model=AppointmentResponse,
    status_code=201,
    responses={
        400: {"description": "Запись не соответствует условиям бронирования"},
        404: {"description": "Мастер или услуга не найдены"},
        409: {"description": "Выбранное время уже занято"},
        422: {"description": "Некорректные данные запроса"},
    },
)
def create_appointment(payload: AppointmentCreate):
    client_name = payload.client_name.strip()

    if not client_name:
        raise HTTPException(
            status_code=422,
            detail="Имя клиента не должно быть пустым",
        )

    # В учебной базе время хранится без часового пояса.
    if payload.start_time.tzinfo is not None:
        raise HTTPException(
            status_code=422,
            detail="Укажите местное время без часового пояса",
        )

    if payload.start_time.second or payload.start_time.microsecond:
        raise HTTPException(
            status_code=422,
            detail="Время начала должно быть указано с точностью до минуты",
        )

    start_at = datetime.combine(
        payload.appointment_date,
        payload.start_time,
    )

    try:
        with psycopg.connect(
            database_url,
            row_factory=dict_row,
        ) as conn:
            with conn.cursor() as cur:

                # Блокируем строку мастера до завершения транзакции.
                # Другой запрос через этот endpoint для того же
                # мастера дождётся завершения текущего запроса.
                cur.execute("""
                    SELECT id
                    FROM masters
                    WHERE id = %s
                    FOR UPDATE
                """, (payload.master_id,))

                if cur.fetchone() is None:
                    raise HTTPException(
                        status_code=404,
                        detail="Мастер не найден",
                    )

                # Проверяем после ожидания блокировки.
                # Пока используем местное время компьютера сервера.
                if start_at <= datetime.now():
                    raise HTTPException(
                        status_code=400,
                        detail="Нельзя создать запись на прошедшее время",
                    )

                cur.execute("""
                    SELECT id, duration_minutes
                    FROM services
                    WHERE id = %s
                """, (payload.service_id,))

                service = cur.fetchone()

                if service is None:
                    raise HTTPException(
                        status_code=404,
                        detail="Услуга не найдена",
                    )

                cur.execute("""
                    SELECT 1
                    FROM master_services
                    WHERE master_id = %s
                      AND service_id = %s
                """, (
                    payload.master_id,
                    payload.service_id,
                ))

                if cur.fetchone() is None:
                    raise HTTPException(
                        status_code=400,
                        detail="Мастер не выполняет выбранную услугу",
                    )

                duration_minutes = service["duration_minutes"]

                if duration_minutes <= 0:
                    raise HTTPException(
                        status_code=500,
                        detail="У услуги некорректная длительность",
                    )

                end_at = start_at + timedelta(
                    minutes=duration_minutes,
                )

                cur.execute("""
                    SELECT start_time, end_time
                    FROM working_hours
                    WHERE master_id = %s
                      AND weekday = %s
                """, (
                    payload.master_id,
                    payload.appointment_date.isoweekday(),
                ))

                hours = cur.fetchone()

                if hours is None:
                    raise HTTPException(
                        status_code=400,
                        detail="Мастер не работает в выбранный день",
                    )

                work_start = datetime.combine(
                    payload.appointment_date,
                    hours["start_time"],
                )
                work_end = datetime.combine(
                    payload.appointment_date,
                    hours["end_time"],
                )

                if start_at < work_start or end_at > work_end:
                    raise HTTPException(
                        status_code=400,
                        detail="Услуга не помещается в рабочие часы мастера",
                    )

                # Проверяем любое пересечение, а не только
                # совпадение времени начала.
                cur.execute("""
                    SELECT id
                    FROM appointments
                    WHERE master_id = %s
                      AND appointment_date = %s
                      AND status <> 'cancelled'
                      AND start_time < %s
                      AND end_time > %s
                    LIMIT 1
                """, (
                    payload.master_id,
                    payload.appointment_date,
                    end_at.time(),
                    start_at.time(),
                ))

                if cur.fetchone() is not None:
                    raise HTTPException(
                        status_code=409,
                        detail="Выбранное время уже занято",
                    )

                cur.execute("""
                    INSERT INTO appointments (
                        master_id,
                        service_id,
                        appointment_date,
                        start_time,
                        end_time,
                        client_name,
                        status
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, 'booked')
                    RETURNING
                        id,
                        master_id,
                        service_id,
                        appointment_date,
                        start_time,
                        end_time,
                        client_name,
                        status
                """, (
                    payload.master_id,
                    payload.service_id,
                    payload.appointment_date,
                    start_at.time(),
                    end_at.time(),
                    client_name,
                ))

                appointment = cur.fetchone()

        # К этому моменту транзакция успешно сохранена.
        return appointment

    except UniqueViolation:
        raise HTTPException(
            status_code=409,
            detail="На это время уже существует запись",
        )
@app.patch(
    "/appointments/{appointment_id}/cancel",
    response_model=AppointmentResponse,
    responses={
        404: {"description": "Запись не найдена"},
        409: {"description": "Завершённую запись нельзя отменить"},
    },
)
def cancel_appointment(appointment_id: int):
    with psycopg.connect(
        database_url,
        row_factory=dict_row,
    ) as conn:
        with conn.cursor() as cur:

            # Сначала узнаём, какому мастеру принадлежит запись.
            cur.execute("""
                SELECT master_id
                FROM appointments
                WHERE id = %s
            """, (appointment_id,))

            existing = cur.fetchone()

            if existing is None:
                raise HTTPException(
                    status_code=404,
                    detail="Запись не найдена",
                )

            # Используем тот же порядок блокировок,
            # что и при создании записи: сначала мастер.
            cur.execute("""
                SELECT id
                FROM masters
                WHERE id = %s
                FOR UPDATE
            """, (existing["master_id"],))

            cur.fetchone()

            # Повторно читаем запись после получения блокировки.
            cur.execute("""
                SELECT
                    id,
                    master_id,
                    service_id,
                    appointment_date,
                    start_time,
                    end_time,
                    client_name,
                    status
                FROM appointments
                WHERE id = %s
                FOR UPDATE
            """, (appointment_id,))

            appointment = cur.fetchone()

            if appointment is None:
                raise HTTPException(
                    status_code=404,
                    detail="Запись не найдена",
                )

            if appointment["status"] == "completed":
                raise HTTPException(
                    status_code=409,
                    detail="Завершённую запись нельзя отменить",
                )

            # Повторная отмена безопасна:
            # возвращаем уже отменённую запись без изменений.
            if appointment["status"] != "cancelled":
                cur.execute("""
                    UPDATE appointments
                    SET status = 'cancelled'
                    WHERE id = %s
                    RETURNING
                        id,
                        master_id,
                        service_id,
                        appointment_date,
                        start_time,
                        end_time,
                        client_name,
                        status
                """, (appointment_id,))

                appointment = cur.fetchone()

    return appointment
@app.get(
    "/appointments/{appointment_id}",
    response_model=AppointmentResponse,
    responses={
        404: {"description": "Запись не найдена"},
    },
)
def get_appointment(appointment_id: int):
    with psycopg.connect(
        database_url,
        row_factory=dict_row,
    ) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    id,
                    master_id,
                    service_id,
                    appointment_date,
                    start_time,
                    end_time,
                    client_name,
                    status
                FROM appointments
                WHERE id = %s
            """, (appointment_id,))

            appointment = cur.fetchone()

            if appointment is None:
                raise HTTPException(
                    status_code=404,
                    detail="Запись не найдена",
                )

    return appointment
class AppointmentReschedule(BaseModel):
    appointment_date: date
    start_time: time


@app.patch(
    "/appointments/{appointment_id}/reschedule",
    response_model=AppointmentResponse,
    responses={
        400: {
            "description": "Новое время не соответствует условиям записи",
        },
        404: {
            "description": "Запись не найдена",
        },
        409: {
            "description": "Время занято или запись нельзя перенести",
        },
        422: {
            "description": "Некорректные данные запроса",
        },
    },
)
def reschedule_appointment(
    appointment_id: int,
    payload: AppointmentReschedule,
):
    if payload.start_time.tzinfo is not None:
        raise HTTPException(
            status_code=422,
            detail="Укажите местное время без часового пояса",
        )

    if payload.start_time.second or payload.start_time.microsecond:
        raise HTTPException(
            status_code=422,
            detail="Время начала должно быть указано с точностью до минуты",
        )

    start_at = datetime.combine(
        payload.appointment_date,
        payload.start_time,
    )

    try:
        with psycopg.connect(
            database_url,
            row_factory=dict_row,
        ) as conn:
            with conn.cursor() as cur:

                # Узнаём, к какому мастеру относится запись.
                cur.execute("""
                    SELECT master_id
                    FROM appointments
                    WHERE id = %s
                """, (appointment_id,))

                existing = cur.fetchone()

                if existing is None:
                    raise HTTPException(
                        status_code=404,
                        detail="Запись не найдена",
                    )

                master_id = existing["master_id"]

                # Используем ту же блокировку мастера,
                # что и при создании записи.
                cur.execute("""
                    SELECT id
                    FROM masters
                    WHERE id = %s
                    FOR UPDATE
                """, (master_id,))

                if cur.fetchone() is None:
                    raise HTTPException(
                        status_code=404,
                        detail="Мастер не найден",
                    )

                # После ожидания блокировки заново читаем запись
                # и блокируем её до конца транзакции.
                cur.execute("""
                    SELECT
                        id,
                        master_id,
                        service_id,
                        status
                    FROM appointments
                    WHERE id = %s
                    FOR UPDATE
                """, (appointment_id,))

                appointment = cur.fetchone()

                if appointment is None:
                    raise HTTPException(
                        status_code=404,
                        detail="Запись не найдена",
                    )

                if appointment["master_id"] != master_id:
                    raise HTTPException(
                        status_code=409,
                        detail="Запись изменилась. Повторите запрос",
                    )

                if appointment["status"] != "booked":
                    raise HTTPException(
                        status_code=409,
                        detail="Переносить можно только действующую запись",
                    )

                # Пока используем местное время сервера,
                # как и при создании записи.
                if start_at <= datetime.now():
                    raise HTTPException(
                        status_code=400,
                        detail="Нельзя перенести запись на прошедшее время",
                    )

                cur.execute("""
                    SELECT duration_minutes
                    FROM services
                    WHERE id = %s
                """, (appointment["service_id"],))

                service = cur.fetchone()

                if service is None:
                    raise HTTPException(
                        status_code=404,
                        detail="Услуга не найдена",
                    )

                if service["duration_minutes"] <= 0:
                    raise HTTPException(
                        status_code=500,
                        detail="У услуги некорректная длительность",
                    )

                end_at = start_at + timedelta(
                    minutes=service["duration_minutes"],
                )

                cur.execute("""
                    SELECT start_time, end_time
                    FROM working_hours
                    WHERE master_id = %s
                      AND weekday = %s
                """, (
                    master_id,
                    payload.appointment_date.isoweekday(),
                ))

                hours = cur.fetchone()

                if hours is None:
                    raise HTTPException(
                        status_code=400,
                        detail="Мастер не работает в выбранный день",
                    )

                work_start = datetime.combine(
                    payload.appointment_date,
                    hours["start_time"],
                )
                work_end = datetime.combine(
                    payload.appointment_date,
                    hours["end_time"],
                )

                if start_at < work_start or end_at > work_end:
                    raise HTTPException(
                        status_code=400,
                        detail="Услуга не помещается в рабочие часы мастера",
                    )

                # Исключаем саму переносимую запись:
                # она не должна конфликтовать сама с собой.
                cur.execute("""
                    SELECT id
                    FROM appointments
                    WHERE master_id = %s
                      AND appointment_date = %s
                      AND status <> 'cancelled'
                      AND id <> %s
                      AND start_time < %s
                      AND end_time > %s
                    LIMIT 1
                """, (
                    master_id,
                    payload.appointment_date,
                    appointment_id,
                    end_at.time(),
                    start_at.time(),
                ))

                if cur.fetchone() is not None:
                    raise HTTPException(
                        status_code=409,
                        detail="Выбранное время уже занято",
                    )

                # Меняем запись только после всех проверок.
                cur.execute("""
                    UPDATE appointments
                    SET appointment_date = %s,
                        start_time = %s,
                        end_time = %s
                    WHERE id = %s
                    RETURNING
                        id,
                        master_id,
                        service_id,
                        appointment_date,
                        start_time,
                        end_time,
                        client_name,
                        status
                """, (
                    payload.appointment_date,
                    start_at.time(),
                    end_at.time(),
                    appointment_id,
                ))

                updated_appointment = cur.fetchone()

        return updated_appointment

    except UniqueViolation:
        raise HTTPException(
            status_code=409,
            detail="На это время уже существует запись",
        )
def cancel_appointment_for_chat(
    appointment_id: int,
    confirmed: bool = False,
):
    with psycopg.connect(
        database_url,
        row_factory=dict_row,
    ) as conn:
        with conn.cursor() as cur:
            # Узнаём мастера, чтобы соблюдать тот же порядок
            # блокировок, что при создании и переносе записи.
            cur.execute("""
                SELECT master_id
                FROM appointments
                WHERE id = %s
            """, (appointment_id,))

            existing = cur.fetchone()

            if existing is None:
                raise HTTPException(
                    status_code=404,
                    detail="Запись не найдена",
                )

            master_id = existing["master_id"]

            cur.execute("""
                SELECT id
                FROM masters
                WHERE id = %s
                FOR UPDATE
            """, (master_id,))

            if cur.fetchone() is None:
                raise HTTPException(
                    status_code=404,
                    detail="Мастер не найден",
                )

            # После ожидания блокировки читаем актуальные данные.
            cur.execute("""
                SELECT
                    a.id,
                    a.master_id,
                    a.appointment_date,
                    a.start_time,
                    a.end_time,
                    a.client_name,
                    a.status,
                    m.name AS master_name,
                    s.name AS service_name
                FROM appointments AS a
                JOIN masters AS m ON m.id = a.master_id
                JOIN services AS s ON s.id = a.service_id
                WHERE a.id = %s
                FOR UPDATE OF a
            """, (appointment_id,))

            appointment = cur.fetchone()

            if appointment is None:
                raise HTTPException(
                    status_code=404,
                    detail="Запись не найдена",
                )

            if appointment["master_id"] != master_id:
                raise HTTPException(
                    status_code=409,
                    detail="Запись изменилась. Повторите запрос",
                )

            # Повторная отмена безопасна: ничего не меняем.
            if appointment["status"] == "cancelled":
                return appointment

            if appointment["status"] != "booked":
                raise HTTPException(
                    status_code=409,
                    detail="Отменить можно только действующую запись",
                )

            if confirmed:
                cur.execute("""
                    UPDATE appointments
                    SET status = 'cancelled'
                    WHERE id = %s
                """, (appointment_id,))

                appointment["status"] = "cancelled"

    return appointment

from salon_chat import install_chat

install_chat(
    app=app,
    project_dir=project_dir,
    get_services=get_services,
    get_masters_for_service=get_masters_for_service,
    get_master_availability=get_master_availability,
    create_appointment=create_appointment,
    appointment_model=AppointmentCreate,
    database_url=database_url,
)
@app.get(
    "/appointments/{appointment_id}",
    response_model=AppointmentResponse,
)
def get_appointment(appointment_id: int):
    with psycopg.connect(
        database_url,
        row_factory=dict_row,
    ) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT *
                FROM appointments
                WHERE id = %s
            """, (appointment_id,))

            appointment = cur.fetchone()

            if appointment is None:
                raise HTTPException(
                    status_code=404,
                    detail="Запись не найдена",
                )

    return appointment


@app.post(
    "/appointments/{appointment_id}/cancel",
    response_model=AppointmentResponse,
)
def cancel_appointment(appointment_id: int):
    with psycopg.connect(
        database_url,
        row_factory=dict_row,
    ) as conn:
        with conn.cursor() as cur:

            # Меняем статус только у активной записи.
            # UPDATE блокирует изменяемую строку
            # до завершения транзакции.
            cur.execute("""
                UPDATE appointments
                SET status = 'cancelled'
                WHERE id = %s
                  AND status = 'booked'
                RETURNING *
            """, (appointment_id,))

            appointment = cur.fetchone()

            if appointment is None:
                # Выясняем, почему запись не обновилась:
                # её нет, она уже отменена или имеет другой статус.
                cur.execute("""
                    SELECT *
                    FROM appointments
                    WHERE id = %s
                """, (appointment_id,))

                appointment = cur.fetchone()

                if appointment is None:
                    raise HTTPException(
                        status_code=404,
                        detail="Запись не найдена",
                    )

                if appointment["status"] != "cancelled":
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "Отмена записи с текущим статусом "
                            "не поддерживается"
                        ),
                    )

    # Здесь транзакция уже успешно сохранена.
    return appointment