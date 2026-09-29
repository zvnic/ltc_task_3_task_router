from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    app_env: str = "production"
    log_level: str = "INFO"
    # Password gate is off by default (public demo). Set AUTH_ENABLED=true and
    # APP_PASSWORD to require login again.
    auth_enabled: bool = False
    app_password: str = ""
    auth_secret: str = ""
    database_url: str = "postgresql+asyncpg://beeline_routes:beeline_routes@postgres/beeline_routes"
    test_database_url: str | None = None
    planning_timeout_seconds: int = Field(default=45, ge=5, le=60)
    solver_time_limit_seconds: int = Field(default=10, ge=1, le=20)
    planning_buffer_minutes: int = Field(default=0, ge=0, le=30)
    objective_mode: Literal["lexicographic", "cost"] = "lexicographic"
    # Норматив времени дороги на одно плечо (раздел «Нормативы»):
    #   "advisory" — справочный: плечо любой длины допустимо, минуты сверх норматива
    #            считаются (PlanMetrics.norm_excess_minutes) и показываются диспетчеру,
    #            но цель их не штрафует;
    #   "soft" — плечо дольше норматива допустимо до потолка (норматив ×
    #            TRAVEL_NORM_MAX_FACTOR), минуты сверх норматива штрафуются в цели; плечо
    #            длиннее потолка недопустимо, как в hard;
    #   "hard" — плечо дольше норматива недопустимо, заявка уходит в отказ.
    # По умолчанию "advisory" — так норматив описали организаторы в чате задачи: 20 минут
    # бронируют время в графике при постановке заявки, а при распределении заменяются
    # расчётным временем в пути (21.09); отправить московскую бригаду в Домодедово или
    # Каширу — не ошибка, специально запрещать это не нужно (22.09). В soft ×2 все 32
    # заявки Юго-востока в Домодедове, Кашире и Ступине, из них 10 аварий, оставались без
    # бригады. soft и hard — переключатели на случай, если заказчик
    # считает норматив пределом. Пешее плечо длиннее 1000 расчётных метров запрещено всегда.
    travel_norm_mode: Literal["advisory", "soft", "hard"] = "advisory"
    # Потолок плеча в режиме soft: не дольше норматива × множитель (округление вниз до
    # минуты). 1.0 — потолок равен нормативу, и soft ведёт себя ровно как hard.
    travel_norm_max_factor: float = Field(default=2.0, ge=1.0, le=10.0)
    # Демонстрационные цены режима "cost" в условных единицах: реальных тарифов
    # заказчика у нас нет, числа условные и требуют подтверждения. Нормативами
    # они не являются; при оплаченных заранее бригадах cost_per_crew_day = 0.
    cost_per_crew_day: int = Field(default=6000, ge=0)
    cost_per_km: int = Field(default=20, ge=0)
    cost_per_travel_minute: int = Field(default=8, ge=0)
    # Штраф за пропуск заведомо дороже вывода ещё одной бригады: охват заявок не
    # разменивается на экономию смены.
    penalty_unassigned_routine: int = Field(default=15000, ge=0)
    penalty_unassigned_connection: int = Field(default=45000, ge=0)
    penalty_unassigned_emergency: int = Field(default=150000, ge=0)
    # Штраф режима "cost" за минуту дороги сверх норматива (только при
    # TRAVEL_NORM_MODE=soft). ДЕМОНСТРАЦИОННОЕ значение, нормативом не является и
    # требует подтверждения заказчиком наравне с остальными ценами режима. Подобрано
    # так, что минута превышения много дороже минуты в пути (8), а наибольшее
    # допустимое превышение одного плеча дешевле пропуска обычной заявки: при
    # нормативе 20 мин и множителе 2,0 это 20 мин × 300 = 6000 < 15000. При множителе
    # выше 3,5 (для норматива 20 мин) соотношение ломается — цены надо пересмотреть.
    penalty_norm_excess_minute: int = Field(default=300, ge=0)
    map_style_url: str = "https://demotiles.maplibre.org/style.json"
    valhalla_url: str = "https://valhalla1.openstreetmap.de"
    valhalla_client_id: str = "task-router-local-demo"
    valhalla_min_interval_seconds: float = Field(default=1.05, ge=0.0, le=5.0)
    osrm_fallback_url: str | None = "https://routing.openstreetmap.de"
    otp_url: str | None = None
    routing_timeout_seconds: float = Field(default=20.0, ge=1.0, le=60.0)
    routing_route_timeout_seconds: float = Field(default=45.0, ge=5.0, le=55.0)
    routing_retry_attempts: int = Field(default=2, ge=0, le=5)
    routing_retry_backoff_seconds: float = Field(default=1.0, ge=0.0, le=5.0)
    # Поиск координат по адресу для новой заявки в течение дня. Публичный Nominatim
    # (OpenStreetMap) разрешает не больше запроса в секунду, требует узнаваемый
    # User-Agent и запрещает автодополнение: интерфейс ищет только по кнопке «Найти».
    # Пустое значение выключает поиск — адрес берётся из справочника или координаты
    # вводятся вручную.
    geocoder_url: str | None = "https://nominatim.openstreetmap.org"
    geocoder_user_agent: str = (
        "task-router-local-demo (+https://github.com/zvnic/hackathon_beeline_routes)"
    )
    geocoder_min_interval_seconds: float = Field(default=1.05, ge=0.0, le=5.0)
    geocoder_timeout_seconds: float = Field(default=10.0, ge=1.0, le=30.0)
    # Область предпочтения поиска: Москва и область (долгота запад, широта север,
    # долгота восток, широта юг). Адрес за её пределами тоже найдётся, но ниже.
    geocoder_viewbox: str = "35.14,56.96,40.21,54.25"


@lru_cache
def get_settings() -> Settings:
    return Settings()
