"""Lucid vehicle state -> Tesla Owner API JSON.

TeslaMate supports pointing at a third-party API host (TESLA_API_HOST) —
that is how MyTeslaMate and Teslemetry work. So we do not patch TeslaMate
and we do not fork it: we answer the Owner API it already speaks, backed by
the Lucid gRPC API. TeslaMate then does the real logging — drive
segmentation, charge sessions, degradation curves, the 23 Grafana dashboards
— and never knows the car is not a Tesla.

UNITS — the whole reason this file is pure and separately tested.

    Lucid reports        km, km/h, Celsius
    Tesla Owner API      MILES, MPH, Celsius
    TeslaMate then does  Convert.miles_to_km(...) on ingest

So values round-trip km -> mi -> km, and any leak of raw km into a
"miles" field is silently inflated by 1.609 forever after. Confirmed in
teslamate/lib/teslamate/vehicles/vehicle.ex:1653 (odometer) and
lib/teslamate/convert.ex (@km_factor 0.62137119223733). We deliberately use
TeslaMate's own constant so the round trip is exact to their precision.

the maintainer already caught one unit bug in this project by eye ("that range isn't
right it's closer to 200 at 50%"). This layer is where the next one would
hide, so every conversion has a test pinned to a real observed value.

SLEEP is the other load-bearing mapping. TeslaMate's state machine keys off
the Owner API's "online" / "asleep". Report online forever and TeslaMate
believes the car never sleeps (polling is harmless — reads never wake a
Lucid — but every parked/idle statistic becomes fiction). Report asleep
wrongly and it logs nothing at all.
"""

from __future__ import annotations

from typing import Any

# TeslaMate's own constant (lib/teslamate/convert.ex). Using theirs rather
# than a rounded 0.621371 keeps km -> mi -> km lossless at their precision.
KM_FACTOR = 0.62137119223733

# Lucid PowerState -> whether the car is meaningfully awake.
# SLEEP and SLEEP_CHARGE are true sleep. SLEEP_UPDATE is asleep while
# installing firmware — still asleep from a driving standpoint.
# CLOUD_1/CLOUD_2 are cloud-side keepalive states, not the car being awake.
_ASLEEP = {
    1,  # POWER_STATE_SLEEP
    6,  # POWER_STATE_SLEEP_CHARGE
    8,  # POWER_STATE_SLEEP_UPDATE
    9,  # POWER_STATE_CLOUD_1
    10,  # POWER_STATE_CLOUD_2
}

# Lucid ChargeState -> Tesla charging_state. TeslaMate opens a charge session
# on "Charging" and closes it on "Complete"/"Disconnected", so only these
# three strings actually drive behaviour; everything else is informational.
_CHARGING = {8}  # CHARGE_STATE_CHARGING
_CHARGE_COMPLETE = {9}  # CHARGE_STATE_CHARGING_END_OK
_CABLE_CONNECTED = {2, 3, 4, 5, 6, 7, 14, 30}


def km_to_mi(km: float | None) -> float | None:
    return None if km is None else km * KM_FACTOR


def bar_to_psi(bar: float | None) -> float | None:
    """Lucid reports tyre pressure in bar; Tesla's tpms_pressure_* are bar too,
    so this exists only for the human-facing extras table."""
    return None if bar is None else bar * 14.5037738


def is_asleep(power_state: int | None) -> bool:
    return power_state in _ASLEEP if power_state is not None else False


def _f(value: Any) -> float | None:
    """Protobuf hands back 0.0 for unset scalars, which is a legitimate value
    for speed and a meaningless one for temperature. We cannot distinguish,
    so we pass floats through and let absent SUB-MESSAGES be the None signal."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _dig(obj: Any, *path: str) -> Any:
    for key in path:
        if obj is None:
            return None
        obj = getattr(obj, key, None)
    return obj


def vehicle_summary(vehicle, tesla_id: int) -> dict[str, Any]:
    """The /api/1/vehicles/{id} and /api/1/products shape.

    TeslaMate filters /products for entries carrying a "vehicle_id" key, so
    that field is not optional — omit it and the car never appears.
    """
    st = vehicle.state
    return {
        "id": tesla_id,
        "vehicle_id": tesla_id,
        "vin": vehicle.config.vin,
        "display_name": vehicle.config.nickname or "Lucid",
        "option_codes": "",
        "color": None,
        "access_type": "OWNER",
        "tokens": [],
        "state": "asleep" if is_asleep(_dig(st, "power")) else "online",
        "in_service": False,
        "id_s": str(tesla_id),
        "calendar_enabled": False,
        "api_version": 71,
        "backseat_token": None,
        "backseat_token_updated_at": None,
    }


def charge_state(st, ts_ms: int) -> dict[str, Any]:
    charging = _dig(st, "charging")
    battery = _dig(st, "battery")
    cs = _dig(charging, "charge_state")

    if cs in _CHARGING:
        charging_state = "Charging"
    elif cs in _CHARGE_COMPLETE:
        charging_state = "Complete"
    elif cs in _CABLE_CONNECTED:
        charging_state = "Stopped"
    else:
        charging_state = "Disconnected"

    soc = _f(_dig(battery, "charge_percent"))
    range_mi = km_to_mi(_f(_dig(battery, "remaining_range")))
    kw = _f(_dig(charging, "charge_rate_kwh_precise"))

    return {
        "battery_level": round(soc) if soc is not None else None,
        "usable_battery_level": round(soc) if soc is not None else None,
        "battery_range": range_mi,
        "est_battery_range": range_mi,
        "ideal_battery_range": range_mi,
        "charging_state": charging_state,
        "charger_power": round(kw) if kw is not None else 0,
        "charge_rate": km_to_mi(_f(_dig(charging, "charge_rate_mph_precise"))),
        "charge_energy_added": _f(_dig(charging, "charge_session_kwh")),
        "charge_miles_added_rated": km_to_mi(_f(_dig(charging, "charge_session_mi"))),
        "charge_miles_added_ideal": km_to_mi(_f(_dig(charging, "charge_session_mi"))),
        "charge_limit_soc": round(_f(_dig(charging, "charge_limit_percent")) or 80),
        "charge_port_door_open": _dig(st, "body", "charge_port") == 1,
        "charger_voltage": 0,
        "charger_actual_current": 0,
        "charger_phases": None,
        "fast_charger_present": False,
        "fast_charger_brand": "<invalid>",
        "fast_charger_type": "<invalid>",
        "conn_charge_cable": "IEC" if cs in _CABLE_CONNECTED else "<invalid>",
        "time_to_full_charge": 0.0,
        "scheduled_charging_pending": False,
        "scheduled_charging_start_time": None,
        "battery_heater_on": False,
        "not_enough_power_to_heat": None,
        "trip_charging": False,
        "timestamp": ts_ms,
    }


# Physically possible ambient range in Celsius. The car occasionally emits a
# transient spike — on 2026-08-10 it reported exterior_temp 109.6 while the
# cabin read 25.8, and the value corrected itself two minutes later. That came
# from Lucid, not from any conversion here. One such sample is enough to flatten
# a Grafana temperature axis for a whole day, and TeslaMate stores what it is
# given, so an impossible reading is dropped rather than recorded.
TEMP_MIN_C, TEMP_MAX_C = -60.0, 70.0


def _plausible_temp(c: float | None) -> float | None:
    if c is None:
        return None
    return c if TEMP_MIN_C <= c <= TEMP_MAX_C else None


def climate_state(st, ts_ms: int) -> dict[str, Any]:
    cabin = _dig(st, "cabin")
    return {
        "inside_temp": _plausible_temp(_f(_dig(cabin, "interior_temp"))),
        "outside_temp": _plausible_temp(_f(_dig(cabin, "exterior_temp"))),
        "driver_temp_setting": _f(_dig(st, "hvac", "front_left_set_point")),
        "passenger_temp_setting": _f(_dig(st, "hvac", "front_right_set_point")),
        "is_climate_on": _dig(st, "hvac", "power") == 2,
        "is_preconditioning": _dig(st, "battery", "preconditioning_status") == 2,
        "fan_status": 0,
        "seat_heater_left": 0,
        "seat_heater_right": 0,
        "is_front_defroster_on": False,
        "is_rear_defroster_on": False,
        "timestamp": ts_ms,
    }


def drive_state(st, ts_ms: int) -> dict[str, Any]:
    gps = _dig(st, "gps")
    speed_kmh = _f(_dig(st, "chassis", "speed"))
    power_state = _dig(st, "power")
    driving = power_state == 4  # POWER_STATE_DRIVE

    return {
        "latitude": _f(_dig(gps, "location", "latitude")),
        "longitude": _f(_dig(gps, "location", "longitude")),
        "heading": round(_f(_dig(gps, "heading_precise")) or 0),
        # Tesla reports null speed when parked, and TeslaMate treats a numeric
        # speed as motion. Sending 0.0 while parked would look like a stopped
        # car mid-drive rather than a parked one.
        "speed": km_to_mi(speed_kmh) if driving and speed_kmh else None,
        "shift_state": "D" if driving else None,
        "power": 0,
        "timestamp": ts_ms,
        "gps_as_of": ts_ms // 1000,
        "native_location_supported": 1,
        "native_latitude": _f(_dig(gps, "location", "latitude")),
        "native_longitude": _f(_dig(gps, "location", "longitude")),
        "native_type": "wgs",
    }


def vehicle_state(st, vehicle, ts_ms: int) -> dict[str, Any]:
    body = _dig(st, "body")
    chassis = _dig(st, "chassis")

    def door(name: str) -> int:
        # Tesla encodes doors as 0 closed / non-zero open.
        return 0 if _dig(body, name) == 2 else 1

    return {
        "odometer": km_to_mi(_f(_dig(chassis, "odometer_km"))),
        "locked": _dig(body, "door_locks") == 2,
        "df": door("front_left_door"),
        "dr": door("rear_left_door"),
        "pf": door("front_right_door"),
        "pr": door("rear_right_door"),
        "ft": door("front_cargo"),
        "rt": door("rear_cargo"),
        "car_version": _dig(chassis, "software_version") or "unknown",
        "vehicle_name": vehicle.config.nickname or "Lucid",
        "sentry_mode": _dig(st, "sentry_state") == 2,
        "is_user_present": False,
        "tpms_pressure_fl": _f(_dig(chassis, "front_left_tire_pressure_bar")),
        "tpms_pressure_fr": _f(_dig(chassis, "front_right_tire_pressure_bar")),
        "tpms_pressure_rl": _f(_dig(chassis, "rear_left_tire_pressure_bar")),
        "tpms_pressure_rr": _f(_dig(chassis, "rear_right_tire_pressure_bar")),
        "api_version": 71,
        "timestamp": ts_ms,
    }


def vehicle_config(ts_ms: int) -> dict[str, Any]:
    """Deliberately an UNRECOGNISED car_type.

    TeslaMate maps car_type -> model ("models" -> "S", "model3" -> "3", ...)
    and anything it does not recognise becomes model = nil
    (vehicles/vehicle.ex identify/1). Its UI renders the model as a hardcoded
    "Model {model}" and skips that block entirely when model is nil — so an
    unknown type shows the car as plain "Gravity" rather than inventing a Tesla.

    The first version of this file sent "models"/"p100d" to keep TeslaMate's
    efficiency lookup happy, and the visible result was the owner's Lucid Gravity
    displayed as a "Model S P100D". Wrong trade: TeslaMate derives efficiency
    empirically from charge data, so the lookup was never load-bearing, and a
    car badged as something it is not is a lie in every screenshot.

    trim_badging is None for the same reason — it is only rendered inside the
    model block, and a stray "P100D" tooltip is the same lie in miniature.
    """
    return {
        "car_type": "lucidgravity",
        "trim_badging": None,
        "exterior_color": None,
        "wheel_type": None,
        "spoiler_type": None,
        "has_air_suspension": True,
        "can_actuate_trunks": True,
        "car_special_type": "base",
        "timestamp": ts_ms,
    }


def vehicle_data(vehicle, tesla_id: int) -> dict[str, Any]:
    """The full /vehicle_data payload TeslaMate polls."""
    st = vehicle.state
    ts_ms = int(getattr(st, "last_updated_ms", 0) or 0)

    payload = vehicle_summary(vehicle, tesla_id)
    payload.update(
        {
            "user_id": tesla_id,
            "charge_state": charge_state(st, ts_ms),
            "climate_state": climate_state(st, ts_ms),
            "drive_state": drive_state(st, ts_ms),
            "vehicle_state": vehicle_state(st, vehicle, ts_ms),
            "vehicle_config": vehicle_config(ts_ms),
            "gui_settings": {
                "gui_distance_units": "mi/hr",
                "gui_temperature_units": "F",
                "gui_charge_rate_units": "kW",
                "gui_24_hour_time": False,
                "gui_range_display": "Rated",
                "timestamp": ts_ms,
            },
        }
    )
    return payload
