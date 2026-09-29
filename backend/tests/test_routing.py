from math import ceil

import pytest

from app.planning.policies import TravelNormPolicy
from app.planning.routing import (
    ACCEPTED_ROUTE_ESTIMATION_METHODS,
    FORMULA_ROUTE_ESTIMATION_METHOD,
    LEGACY_ROUTE_ESTIMATION_METHOD,
    MAX_BICYCLE_LEG_METERS,
    MAX_CAR_LOCAL_WALK_METERS,
    MAX_WALKING_LEG_METERS,
    PREVIOUS_ROUTE_ESTIMATION_METHOD,
    ROUTE_ESTIMATION_METHOD,
    SUBURBAN_BOARDING_MINUTES,
    TRANSPORT_ACCESS_MINUTES,
    TRANSPORT_MIN_TRAVEL_MINUTES,
    TRANSPORT_PARAMETERS,
    edge_is_feasible,
    edge_metrics,
    effective_edge_metrics,
    haversine_meters,
    share_inside_mkad,
    travel_exceeds_norm,
    travel_norm_excess_minutes,
)
from app.schemas import Coordinates, Transport

ORIGIN = Coordinates(latitude=55.75, longitude=37.62)
# ~110 m north — short urban hop
NEAR = Coordinates(latitude=55.751, longitude=37.62)
# ~2.2 km east — medium hop
MID = Coordinates(latitude=55.75, longitude=37.65)
# ~11 km east — long hop where moving time dominates floor
FAR = Coordinates(latitude=55.75, longitude=37.80)
# Офис Юго-востока у МКАД и заявки в области (демо-координаты зоны)
BIRYULYOVO = Coordinates(latitude=55.59, longitude=37.67)
KASHIRA = Coordinates(latitude=54.85, longitude=38.16)
DOMODEDOVO = Coordinates(latitude=55.44, longitude=37.77)


def test_route_estimation_method_is_v6() -> None:
    assert ROUTE_ESTIMATION_METHOD == "road_matrix_by_transport_v6"
    assert FORMULA_ROUTE_ESTIMATION_METHOD == "haversine_by_transport_v5"
    assert PREVIOUS_ROUTE_ESTIMATION_METHOD == "haversine_by_transport_v4"
    assert LEGACY_ROUTE_ESTIMATION_METHOD == "haversine_by_transport_v3"
    assert ACCEPTED_ROUTE_ESTIMATION_METHODS == {
        ROUTE_ESTIMATION_METHOD,
        FORMULA_ROUTE_ESTIMATION_METHOD,
        PREVIOUS_ROUTE_ESTIMATION_METHOD,
        LEGACY_ROUTE_ESTIMATION_METHOD,
    }


def test_v5_changes_only_public_transport() -> None:
    for origin, destination in ((ORIGIN, FAR), (ORIGIN, MID), (BIRYULYOVO, KASHIRA)):
        for transport in (Transport.CAR, Transport.WALKING, Transport.BICYCLE):
            assert edge_metrics(origin, destination, transport) == edge_metrics(
                origin, destination, transport, method=PREVIOUS_ROUTE_ESTIMATION_METHOD
            )


def test_leg_inside_mkad_is_counted_exactly_as_v3() -> None:
    assert share_inside_mkad(ORIGIN, FAR) == 1.0
    for transport in Transport:
        assert edge_metrics(
            ORIGIN, FAR, transport, method=PREVIOUS_ROUTE_ESTIMATION_METHOD
        ) == edge_metrics(ORIGIN, FAR, transport, method=LEGACY_ROUTE_ESTIMATION_METHOD)


def test_leg_beyond_mkad_uses_suburban_speed_for_car_and_transit() -> None:
    assert 0.0 < share_inside_mkad(BIRYULYOVO, KASHIRA) < 0.1
    assert share_inside_mkad(KASHIRA, DOMODEDOVO) == 0.0
    for transport in (Transport.CAR, Transport.PUBLIC_TRANSPORT):
        distance, minutes = edge_metrics(
            BIRYULYOVO, KASHIRA, transport, method=PREVIOUS_ROUTE_ESTIMATION_METHOD
        )
        legacy_distance, legacy_minutes = edge_metrics(
            BIRYULYOVO, KASHIRA, transport, method=LEGACY_ROUTE_ESTIMATION_METHOD
        )
        # расстояние то же, быстрее только время: скорость меняется, дорога — нет
        assert distance == legacy_distance
        assert minutes < legacy_minutes
    _, car_minutes = edge_metrics(BIRYULYOVO, KASHIRA, Transport.CAR)
    # 88 км по прямой: было 4,6 ч по городской скорости, стало около двух часов
    assert 100 <= car_minutes <= 160
    for transport in (Transport.BICYCLE, Transport.WALKING):
        assert edge_metrics(BIRYULYOVO, KASHIRA, transport) == edge_metrics(
            BIRYULYOVO, KASHIRA, transport, method=LEGACY_ROUTE_ESTIMATION_METHOD
        )


def test_transit_to_kashira_matches_yandex_range() -> None:
    # Яндекс, вторник 29.09: от офиса Юго-востока до Каширы 140 мин (v4 давала 182)
    _, minutes = edge_metrics(BIRYULYOVO, KASHIRA, Transport.PUBLIC_TRANSPORT)
    assert 125 <= minutes <= 155


def test_transit_boarding_is_added_only_beyond_mkad() -> None:
    boarding = SUBURBAN_BOARDING_MINUTES[Transport.PUBLIC_TRANSPORT]
    access = TRANSPORT_ACCESS_MINUTES[Transport.PUBLIC_TRANSPORT]
    speed_kmh, _ = TRANSPORT_PARAMETERS[Transport.PUBLIC_TRANSPORT]
    distance, inside = edge_metrics(ORIGIN, FAR, Transport.PUBLIC_TRANSPORT)
    # внутри МКАД: только движение и подход, без посадки на электричку
    assert inside == ceil(distance / 1000 / speed_kmh * 60) + access
    _, beyond = edge_metrics(KASHIRA, DOMODEDOVO, Transport.PUBLIC_TRANSPORT)
    assert beyond >= access + boarding


@pytest.mark.parametrize(("origin", "destination"), [(BIRYULYOVO, KASHIRA), (ORIGIN, FAR)])
def test_share_inside_mkad_is_symmetric(origin: Coordinates, destination: Coordinates) -> None:
    assert share_inside_mkad(origin, destination) == pytest.approx(
        share_inside_mkad(destination, origin)
    )


def test_advisory_policy_sets_no_travel_limit() -> None:
    policy = TravelNormPolicy(hard=False, max_factor=2.0, advisory=True)
    assert policy.limit(20) is None
    assert policy.max_excess(20) == 0
    # превышение по-прежнему считается — для показа диспетчеру
    assert travel_norm_excess_minutes(150, 20) == 130


def test_same_point_is_zero() -> None:
    distance, minutes = edge_metrics(ORIGIN, ORIGIN, Transport.CAR)
    assert distance == 0
    assert minutes == 0
    assert haversine_meters(ORIGIN, ORIGIN) == 0.0


def test_car_short_hop_respects_min_floor() -> None:
    distance, minutes = edge_metrics(ORIGIN, NEAR, Transport.CAR)
    assert distance >= 1
    assert minutes >= TRANSPORT_MIN_TRAVEL_MINUTES[Transport.CAR]
    assert minutes >= TRANSPORT_ACCESS_MINUTES[Transport.CAR]
    # short hop must not collapse to 1–2 minutes
    assert minutes >= 4


def test_car_crew_walks_a_short_local_connector() -> None:
    distance, minutes, mode = effective_edge_metrics(ORIGIN, NEAR, Transport.CAR)
    assert distance <= MAX_CAR_LOCAL_WALK_METERS
    assert mode is Transport.WALKING
    assert (distance, minutes) == edge_metrics(ORIGIN, NEAR, Transport.WALKING)


def test_car_crew_keeps_car_for_a_longer_leg() -> None:
    distance, minutes, mode = effective_edge_metrics(ORIGIN, MID, Transport.CAR)
    assert distance > MAX_CAR_LOCAL_WALK_METERS
    assert mode is Transport.CAR
    assert (distance, minutes) == edge_metrics(ORIGIN, MID, Transport.CAR)


def test_walking_slower_than_car_on_same_edge() -> None:
    _, car_m = edge_metrics(ORIGIN, MID, Transport.CAR)
    _, walk_m = edge_metrics(ORIGIN, MID, Transport.WALKING)
    assert walk_m > car_m


def test_public_transport_short_hop_floor() -> None:
    _, minutes = edge_metrics(ORIGIN, NEAR, Transport.PUBLIC_TRANSPORT)
    assert minutes >= TRANSPORT_MIN_TRAVEL_MINUTES[Transport.PUBLIC_TRANSPORT]
    # поездка даже к соседнему дому — не меньше подхода к остановке и ожидания
    assert minutes >= TRANSPORT_ACCESS_MINUTES[Transport.PUBLIC_TRANSPORT]


def test_transit_crew_walks_when_walking_is_not_slower() -> None:
    distance, minutes, mode = effective_edge_metrics(ORIGIN, NEAR, Transport.PUBLIC_TRANSPORT)
    assert mode is Transport.WALKING
    assert (distance, minutes) == edge_metrics(ORIGIN, NEAR, Transport.WALKING)
    # до калибровки (v4) бригада на общественном транспорте пешком не ходила
    _, _, before = effective_edge_metrics(
        ORIGIN, NEAR, Transport.PUBLIC_TRANSPORT, method=PREVIOUS_ROUTE_ESTIMATION_METHOD
    )
    assert before is Transport.PUBLIC_TRANSPORT


def test_transit_crew_rides_a_longer_leg() -> None:
    distance, minutes, mode = effective_edge_metrics(ORIGIN, MID, Transport.PUBLIC_TRANSPORT)
    assert mode is Transport.PUBLIC_TRANSPORT
    assert (distance, minutes) == edge_metrics(ORIGIN, MID, Transport.PUBLIC_TRANSPORT)
    _, walking_minutes = edge_metrics(ORIGIN, MID, Transport.WALKING)
    assert minutes < walking_minutes


def test_bicycle_has_access_and_floor() -> None:
    _, minutes = edge_metrics(ORIGIN, NEAR, Transport.BICYCLE)
    assert minutes >= TRANSPORT_MIN_TRAVEL_MINUTES[Transport.BICYCLE]
    assert minutes >= TRANSPORT_ACCESS_MINUTES[Transport.BICYCLE]


def test_long_hop_moving_time_dominates_floor() -> None:
    distance, minutes = edge_metrics(ORIGIN, FAR, Transport.CAR)
    # path distance should be several km
    assert distance > 5_000
    # moving + access should exceed the 4-minute floor
    assert minutes > TRANSPORT_MIN_TRAVEL_MINUTES[Transport.CAR]
    # sanity: not an absurd multi-hour hop for ~11 km urban
    assert minutes < 120


def test_distance_applies_path_factor() -> None:
    straight = haversine_meters(ORIGIN, MID)
    distance, _ = edge_metrics(ORIGIN, MID, Transport.CAR)
    assert distance >= int(round(straight))  # path_factor >= 1
    assert distance == max(1, int(round(straight * 1.30)))


def test_long_walking_leg_keeps_primary_mode_but_is_infeasible() -> None:
    walking_distance, walking_minutes = edge_metrics(ORIGIN, MID, Transport.WALKING)
    distance, minutes, mode = effective_edge_metrics(
        ORIGIN,
        MID,
        Transport.WALKING,
    )
    assert walking_distance > MAX_WALKING_LEG_METERS
    assert mode is Transport.WALKING
    assert (distance, minutes) == (walking_distance, walking_minutes)
    assert not edge_is_feasible(mode, distance)


def test_short_walking_leg_is_feasible() -> None:
    distance, minutes, mode = effective_edge_metrics(
        ORIGIN,
        NEAR,
        Transport.WALKING,
    )
    assert mode is Transport.WALKING
    assert (distance, minutes) == edge_metrics(ORIGIN, NEAR, Transport.WALKING)
    assert edge_is_feasible(mode, distance)


def test_bicycle_leg_limit_is_twelve_route_kilometres() -> None:
    assert MAX_BICYCLE_LEG_METERS == 12_000
    assert edge_is_feasible(Transport.BICYCLE, MAX_BICYCLE_LEG_METERS)
    assert not edge_is_feasible(Transport.BICYCLE, MAX_BICYCLE_LEG_METERS + 1)
    # у машины и ОТ предела плеча нет — их ограничивает только норматив дороги
    assert edge_is_feasible(Transport.CAR, 100_000)
    assert edge_is_feasible(Transport.PUBLIC_TRANSPORT, 100_000)


def test_travel_norm_excess_is_strictly_greater_and_mode_free() -> None:
    assert not travel_exceeds_norm(20, None)
    assert not travel_exceeds_norm(20, 20)
    assert travel_exceeds_norm(21, 20)


def test_travel_norm_excess_counts_minutes_over_the_norm() -> None:
    assert travel_norm_excess_minutes(552, None) == 0
    assert travel_norm_excess_minutes(20, 20) == 0
    assert travel_norm_excess_minutes(21, 20) == 1
    # плечо из ревизии: 552 мин при нормативе 20 — это 532 мин превышения, а не «одно»
    assert travel_norm_excess_minutes(552, 20) == 532


def test_travel_norm_limit_is_norm_in_hard_and_ceiling_in_soft() -> None:
    hard = TravelNormPolicy(hard=True, max_factor=2.0)
    assert (hard.limit(20), hard.max_excess(20)) == (20, 0)
    soft = TravelNormPolicy(hard=False, max_factor=2.0)
    assert (soft.limit(20), soft.max_excess(20)) == (40, 20)
    assert (soft.limit(None), soft.max_excess(None)) == (None, 0)
    # до минуты вниз и без хвоста двоичной дроби: 10 × 2,3 = 23, а не 22
    assert TravelNormPolicy(hard=False, max_factor=2.3).limit(10) == 23
    assert TravelNormPolicy(hard=False, max_factor=1.25).limit(10) == 12
    # множитель 1,0: потолок равен нормативу, как в hard
    one = TravelNormPolicy(hard=False, max_factor=1.0)
    assert (one.limit(20), one.max_excess(20)) == (hard.limit(20), 0)


def test_travel_norm_limit_applies_only_when_passed_but_walking_limit_always() -> None:
    distance, minutes, mode = effective_edge_metrics(ORIGIN, FAR, Transport.CAR)
    # предел плеча (hard — норматив, soft — потолок) отбрасывает плечо, без предела — нет
    assert not edge_is_feasible(
        mode,
        distance,
        travel_minutes=minutes,
        max_travel_minutes=minutes - 1,
    )
    assert edge_is_feasible(mode, distance, travel_minutes=minutes, max_travel_minutes=None)
    # пешее плечо длиннее 1000 м запрещено при любом нормативе
    walking_distance, walking_minutes, walking_mode = effective_edge_metrics(
        ORIGIN,
        MID,
        Transport.WALKING,
    )
    assert not edge_is_feasible(
        walking_mode,
        walking_distance,
        travel_minutes=walking_minutes,
        max_travel_minutes=None,
    )
