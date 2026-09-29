from math import asin, ceil, cos, radians, sin, sqrt

from app.planning.road_matrix import road_meters
from app.schemas import Coordinates, Transport

EARTH_RADIUS_METERS = 6_371_000

# (average_speed_kmh, path_factor) — urban field-service model, not free-flow GPS.
# Общественный транспорт откалиброван по Яндекс Картам (v5):
# 23 км/ч в движении — метро и наземный транспорт вместе, без подхода и ожидания.
TRANSPORT_PARAMETERS: dict[Transport, tuple[float, float]] = {
    Transport.CAR: (25.0, 1.30),
    Transport.WALKING: (4.5, 1.15),
    Transport.BICYCLE: (12.0, 1.20),
    Transport.PUBLIC_TRANSPORT: (23.0, 1.30),
}

# Fixed access overhead: park / lock bike / leave stop / enter client premises.
# Общественному транспорту — подход к остановке, ожидание и пересадка: 15 мин по
# 92 городским плечам Яндекса (было 5 — короткие поездки выходили вдвое быстрее).
TRANSPORT_ACCESS_MINUTES: dict[Transport, int] = {
    Transport.CAR: 3,
    Transport.WALKING: 1,
    Transport.BICYCLE: 2,
    Transport.PUBLIC_TRANSPORT: 15,
}

# Hard floor for any non-zero relocation between distinct coordinates.
TRANSPORT_MIN_TRAVEL_MINUTES: dict[Transport, int] = {
    Transport.CAR: 4,
    Transport.WALKING: 3,
    Transport.BICYCLE: 3,
    Transport.PUBLIC_TRANSPORT: 8,
}

# v6: расстояние машины, пешехода и велосипеда между точками зоны — по дорогам
# OpenStreetMap (`app.planning.road_matrix`), время — прежней моделью скорости.
ROUTE_ESTIMATION_METHOD = "road_matrix_by_transport_v6"
# Прежние версии модели. По ним валидатор перепроверяет защищённый префикс плана,
# посчитанного раньше: время тех плеч уже обещано. v5 — всё расстояние по прямой ×
# коэффициент пути, v4 — ещё и общественный транспорт до калибровки, v3 — ещё и без
# загородной скорости.
FORMULA_ROUTE_ESTIMATION_METHOD = "haversine_by_transport_v5"
PREVIOUS_ROUTE_ESTIMATION_METHOD = "haversine_by_transport_v4"
LEGACY_ROUTE_ESTIMATION_METHOD = "haversine_by_transport_v3"
ACCEPTED_ROUTE_ESTIMATION_METHODS = frozenset(
    {
        ROUTE_ESTIMATION_METHOD,
        FORMULA_ROUTE_ESTIMATION_METHOD,
        PREVIOUS_ROUTE_ESTIMATION_METHOD,
        LEGACY_ROUTE_ESTIMATION_METHOD,
    }
)
# Версии с откалиброванным по Яндексу общественным транспортом.
CALIBRATED_TRANSIT_METHODS = frozenset(
    {ROUTE_ESTIMATION_METHOD, FORMULA_ROUTE_ESTIMATION_METHOD}
)

# Загородная скорость, км/ч: часть пути за МКАД машина идёт по трассе, общественный
# транспорт — электричкой или междугородним автобусом. Пешком и на велосипеде
# скорость одна. Без неё до Каширы (88 км по прямой) городская скорость 25 км/ч
# давала 4,6 часа в одну сторону.
SUBURBAN_SPEED_KMH: dict[Transport, float] = {
    Transport.CAR: 60.0,
    Transport.PUBLIC_TRANSPORT: 70.0,
}

# Посадка на загородный транспорт, мин: дойти до платформы или автовокзала и
# дождаться электрички или автобуса. Добавляется, если за МКАД больше
# SUBURBAN_BOARDING_MIN_KM пути. Без неё поездки к городам у МКАД (Видное,
# Дзержинский, Щербинка) выходили в 2–3 раза быстрее, чем в Яндексе.
SUBURBAN_BOARDING_MINUTES: dict[Transport, int] = {
    Transport.PUBLIC_TRANSPORT: 25,
}
SUBURBAN_BOARDING_MIN_KM = 0.5

# Общественный транспорт в v4 и v3 — до калибровки: 15 км/ч, 5 минут подхода,
# за МКАД 40 км/ч (v4) без посадки. Нужен только для перепроверки старых плеч.
PUBLIC_TRANSPORT_BEFORE_V5_SPEED_KMH = 15.0
PUBLIC_TRANSPORT_BEFORE_V5_ACCESS_MINUTES = 5
PUBLIC_TRANSPORT_BEFORE_V5_SUBURBAN_SPEED_KMH = 40.0

# МКАД в модели — эллипс вокруг центра кольца: полуоси 18,8 км (север — юг) и
# 14,9 км (запад — восток) по крайним точкам кольца. Внутри действует городская
# скорость, поэтому плечи внутри Москвы v4 считает ровно как v3.
MKAD_CENTER_LATITUDE = 55.742
MKAD_CENTER_LONGITUDE = 37.6055
MKAD_SEMI_AXIS_NORTH_KM = 18.8
MKAD_SEMI_AXIS_EAST_KM = 14.9
KM_PER_DEGREE_LATITUDE = 111.32

MAX_WALKING_LEG_METERS = 1_000
# Велосипедное плечо длиннее 12 расчётных км (около 10 км по прямой, час в пути) бригаде
# не поручается. Без предела решатель отправлял велосипед из офиса в Домодедово — 26 км,
# 135 минут. Замер 28.09 на Юго-востоке: с пределом назначено 76 заявок против 75,
# бригад 11 против 12, пробег 630 км против 710.
MAX_BICYCLE_LEG_METERS = 12_000
# Предел одного плеча в расчётных метрах по основному транспорту бригады.
LEG_LIMIT_METERS: dict[Transport, int] = {
    Transport.WALKING: MAX_WALKING_LEG_METERS,
    Transport.BICYCLE: MAX_BICYCLE_LEG_METERS,
}
# A car crew parks once inside a dense local cluster and walks to the next
# nearby building instead of making a fictitious micro-trip by car.
MAX_CAR_LOCAL_WALK_METERS = 500


def haversine_meters(origin: Coordinates, destination: Coordinates) -> float:
    if origin == destination:
        return 0.0
    lat1, lon1, lat2, lon2 = map(
        radians,
        [origin.latitude, origin.longitude, destination.latitude, destination.longitude],
    )
    delta_lat = lat2 - lat1
    delta_lon = lon2 - lon1
    angle = sin(delta_lat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(delta_lon / 2) ** 2
    return 2 * EARTH_RADIUS_METERS * asin(sqrt(angle))


def share_inside_mkad(origin: Coordinates, destination: Coordinates) -> float:
    """Доля отрезка origin → destination, лежащая внутри МКАД (эллипс модели).

    Растяжение осей переводит эллипс в единичный круг и сохраняет доли длины на
    отрезке, поэтому достаточно пересечь отрезок с единичной окружностью. Эллипс
    выпуклый: плечо между двумя точками внутри МКАД лежит внутри целиком (доля 1).
    """

    def normalized(point: Coordinates) -> tuple[float, float]:
        north_km = (point.latitude - MKAD_CENTER_LATITUDE) * KM_PER_DEGREE_LATITUDE
        east_km = (
            (point.longitude - MKAD_CENTER_LONGITUDE)
            * KM_PER_DEGREE_LATITUDE
            * cos(radians(MKAD_CENTER_LATITUDE))
        )
        return east_km / MKAD_SEMI_AXIS_EAST_KM, north_km / MKAD_SEMI_AXIS_NORTH_KM

    x0, y0 = normalized(origin)
    x1, y1 = normalized(destination)
    dx, dy = x1 - x0, y1 - y0
    a = dx * dx + dy * dy
    if a == 0:
        return 1.0 if x0 * x0 + y0 * y0 <= 1 else 0.0
    b = 2 * (x0 * dx + y0 * dy)
    c = x0 * x0 + y0 * y0 - 1
    discriminant = b * b - 4 * a * c
    if discriminant <= 0:
        return 0.0
    root = sqrt(discriminant)
    enter = max(0.0, (-b - root) / (2 * a))
    leave = min(1.0, (-b + root) / (2 * a))
    return max(0.0, leave - enter)


def edge_metrics(
    origin: Coordinates,
    destination: Coordinates,
    transport: Transport,
    *,
    method: str = ROUTE_ESTIMATION_METHOD,
) -> tuple[int, int]:
    """Return (distance_meters, travel_minutes) for one relocation.

    С v6 расстояние между точками одной зоны — по сети дорог OpenStreetMap для
    машины, пешехода и велосипеда (`road_meters`), но не меньше прямой. Остальное —
    новый адрес вне справочника, общественный транспорт, версии до v6 — haversine ×
    path_factor (road/path detour), rounded to int meters.
    Travel time uses transport speed, plus access overhead and a per-transport floor
    so dense nearby addresses cannot collapse to 1 minute for a car crew.
    Same-point edges stay 0/0.

    Часть пути за МКАД машина и общественный транспорт проходят с загородной
    скоростью (`SUBURBAN_SPEED_KMH`), общественный транспорт — ещё и с посадкой
    на электричку (`SUBURBAN_BOARDING_MINUTES`). `method` — версия модели: по v4
    и v3 валидатор перепроверяет плечи, посчитанные до смены модели.
    """
    if origin == destination:
        return 0, 0

    speed_kmh, path_factor = TRANSPORT_PARAMETERS[transport]
    access_minutes = TRANSPORT_ACCESS_MINUTES[transport]
    min_travel_minutes = TRANSPORT_MIN_TRAVEL_MINUTES[transport]
    suburban_speed_kmh = SUBURBAN_SPEED_KMH.get(transport)
    boarding_minutes = SUBURBAN_BOARDING_MINUTES.get(transport, 0)
    if (
        transport is Transport.PUBLIC_TRANSPORT
        and method not in CALIBRATED_TRANSIT_METHODS
    ):
        speed_kmh = PUBLIC_TRANSPORT_BEFORE_V5_SPEED_KMH
        access_minutes = PUBLIC_TRANSPORT_BEFORE_V5_ACCESS_MINUTES
        suburban_speed_kmh = PUBLIC_TRANSPORT_BEFORE_V5_SUBURBAN_SPEED_KMH
        boarding_minutes = 0
    if method == LEGACY_ROUTE_ESTIMATION_METHOD:
        suburban_speed_kmh = None

    straight_meters = haversine_meters(origin, destination)
    road = (
        road_meters(origin, destination, transport)
        if method == ROUTE_ESTIMATION_METHOD
        else None
    )
    if road is not None:
        # OSRM меряет между точками, притянутыми к дороге: у домов на одной улице
        # выходит ноль, поэтому не меньше прямой
        distance_meters = max(1, road, int(round(straight_meters)))
    else:
        distance_meters = max(1, int(round(straight_meters * path_factor)))
    distance_km = distance_meters / 1000.0
    if suburban_speed_kmh is None:
        moving_hours = distance_km / speed_kmh
    else:
        urban_km = distance_km * share_inside_mkad(origin, destination)
        suburban_km = distance_km - urban_km
        moving_hours = urban_km / speed_kmh + suburban_km / suburban_speed_kmh
        if suburban_km > SUBURBAN_BOARDING_MIN_KM:
            access_minutes += boarding_minutes
    moving_minutes = ceil(moving_hours * 60.0)
    travel_minutes = max(min_travel_minutes, moving_minutes + access_minutes)
    return distance_meters, int(travel_minutes)


def effective_edge_metrics(
    origin: Coordinates,
    destination: Coordinates,
    primary_transport: Transport,
    *,
    method: str = ROUTE_ESTIMATION_METHOD,
) -> tuple[int, int, Transport]:
    """Return metrics and the effective mode for one scheduled leg.

    A walking engineer is never silently switched to public transport. A car
    crew may make a short local pedestrian connector after parking; the
    effective mode is stored on the leg. Feasibility of a walking-only engineer
    edge is checked separately by ``edge_is_feasible``.

    Бригада на общественном транспорте с v5 идёт пешком, если так не дольше:
    ждать автобус ради соседнего дома никто не станет. Яндекс в таких случаях
    тоже предлагает пеший маршрут.
    """
    distance_meters, travel_minutes = edge_metrics(
        origin,
        destination,
        primary_transport,
        method=method,
    )
    if (
        primary_transport is Transport.CAR
        and 0 < distance_meters <= MAX_CAR_LOCAL_WALK_METERS
    ):
        walking_distance, walking_minutes = edge_metrics(
            origin,
            destination,
            Transport.WALKING,
            method=method,
        )
        return walking_distance, walking_minutes, Transport.WALKING
    if (
        primary_transport is Transport.PUBLIC_TRANSPORT
        and method in CALIBRATED_TRANSIT_METHODS
        and travel_minutes > 0
    ):
        walking_distance, walking_minutes = edge_metrics(
            origin,
            destination,
            Transport.WALKING,
            method=method,
        )
        if walking_minutes <= travel_minutes:
            return walking_distance, walking_minutes, Transport.WALKING
    return distance_meters, travel_minutes, primary_transport


def travel_exceeds_norm(travel_minutes: int, norm_minutes: int | None) -> bool:
    """Доехали ли до заявки дольше норматива времени дороги.

    Чистый расчёт без режимов: превышение считается одинаково и там, где оно
    запрещено (TRAVEL_NORM_MODE=hard), и там, где штрафуется (soft) или только
    показывается диспетчеру (advisory).
    """
    return norm_minutes is not None and travel_minutes > norm_minutes


def travel_norm_excess_minutes(travel_minutes: int, norm_minutes: int | None) -> int:
    """Сколько минут дороги сверх норматива: max(0, дорога − норматив), без норматива 0.

    Величина превышения, а не его факт: её штрафует цель в режиме soft, иначе
    решатель покупает охват плечами в несколько часов.
    """
    if norm_minutes is None or travel_minutes <= norm_minutes:
        return 0
    return travel_minutes - norm_minutes


def edge_is_feasible(
    transport: Transport,
    distance_meters: int,
    *,
    travel_minutes: int | None = None,
    max_travel_minutes: int | None = None,
) -> bool:
    """Return whether one relocation is operationally allowed for the mode.

    Пешее плечо длиннее 1000 расчётных метров и велосипедное длиннее 12 км — физика,
    запрет в любом режиме (``LEG_LIMIT_METERS``).
    ``max_travel_minutes`` — предел плеча по нормативу дороги
    (``app.planning.policies.TravelNormPolicy.limit``): в режиме hard это сам норматив,
    в режиме soft — потолок «норматив × множитель», в режиме advisory предела нет;
    превышение считается отдельно (``travel_norm_excess_minutes``).
    """
    leg_limit = LEG_LIMIT_METERS.get(transport)
    if leg_limit is not None and distance_meters > leg_limit:
        return False
    return not (
        max_travel_minutes is not None
        and travel_minutes is not None
        and travel_minutes > max_travel_minutes
    )
