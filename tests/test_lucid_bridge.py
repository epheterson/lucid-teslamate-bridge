"""Tests for the Lucid -> Tesla Owner API bridge.

Two classes of bug here are unrecoverable once TeslaMate has ingested them,
because TeslaMate stores derived values (drives, charges, efficiency) rather
than re-deriving from raw:

  UNITS. TeslaMate calls Convert.miles_to_km() on everything we send. Leak a
  raw kilometre into a miles field and every odometer, range, and efficiency
  figure is inflated by 1.609 permanently. Values below are pinned to what
  the real car reported on 2026-08-09 (191.9 km / 315 km at 51%), and to
  the owner's own eyeball check of ~200 mi at half charge.

  SLEEP. TeslaMate's state machine branches on "online" vs "asleep". Wrong
  in one direction and every idle statistic is fiction; wrong in the other
  and it records nothing at all.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "docker" / "lucid-bridge"))

import mapping  # noqa: E402


def _vehicle(
    *,
    power=1,
    odometer_km=191.9,
    range_km=315.0,
    soc=50.8,
    charge_state=1,
    speed=0.0,
):
    from lucidmotors.gen.vehicle_state_service_pb2 import Vehicle

    v = Vehicle()
    v.config.vin = "5UXCR6C0XL9XXXXXX"
    v.config.nickname = "Gravity"
    v.state.last_updated_ms = 1_786_339_000_000
    v.state.power = power
    v.state.battery.charge_percent = soc
    v.state.battery.remaining_range = range_km
    v.state.charging.charge_state = charge_state
    v.state.charging.charge_limit_percent = 80.0
    v.state.chassis.odometer_km = odometer_km
    v.state.chassis.speed = speed
    v.state.chassis.software_version = "3.6.3"
    v.state.cabin.interior_temp = 29.6
    v.state.cabin.exterior_temp = 26.9
    v.state.gps.location.latitude = 37.77490000000000
    v.state.gps.location.longitude = -122.41940000000000
    v.state.body.door_locks = 2
    v.state.body.front_left_door = 2
    return v


# ── units: pinned to the real car ─────────────────────────────────────────


def test_odometer_is_converted_km_to_miles():
    """191.9 km on the real car = 119.2 mi. Sending 191.9 as "miles" would
    make TeslaMate store 308.8 km."""
    vs = mapping.vehicle_state(_vehicle().state, _vehicle(), 0)
    assert vs["odometer"] == pytest.approx(119.24, abs=0.05)
    assert vs["odometer"] != pytest.approx(191.9)


def test_range_is_converted_and_matches_the_dash():
    """315 km at 51% — the maintainer: "closer to 200 at 50%"."""
    cs = mapping.charge_state(_vehicle().state, 0)
    assert cs["battery_range"] == pytest.approx(196, abs=1)


def test_round_trip_through_teslamates_own_constant_is_lossless():
    """We send miles; TeslaMate divides by the same factor to get km back.
    Using a rounded constant here would drift the odometer over time."""
    km = 191.9
    mi = mapping.km_to_mi(km)
    assert mi / mapping.KM_FACTOR == pytest.approx(km, abs=1e-9)


def test_temperatures_pass_through_as_celsius():
    """Tesla's API is Celsius too — converting here would double-convert."""
    cl = mapping.climate_state(_vehicle().state, 0)
    assert cl["inside_temp"] == pytest.approx(29.6)
    assert cl["outside_temp"] == pytest.approx(26.9)


# ── sleep: the other load-bearing mapping ─────────────────────────────────


@pytest.mark.parametrize(
    "power,expected", [(1, "asleep"), (6, "asleep"), (8, "asleep")]
)
def test_sleep_states_report_asleep(power, expected):
    assert mapping.vehicle_summary(_vehicle(power=power), 1)["state"] == expected


@pytest.mark.parametrize("power", [2, 3, 4, 5, 7, 11])
def test_awake_states_report_online(power):
    assert mapping.vehicle_summary(_vehicle(power=power), 1)["state"] == "online"


def test_unknown_power_state_defaults_to_online():
    """Better to poll a sleeping car (reads never wake a Lucid) than to
    silently stop logging because of an unrecognised enum."""
    assert mapping.vehicle_summary(_vehicle(power=0), 1)["state"] == "online"


# ── charging ──────────────────────────────────────────────────────────────


def test_charging_opens_a_session():
    assert (
        mapping.charge_state(_vehicle(charge_state=8).state, 0)["charging_state"]
        == "Charging"
    )


def test_charge_complete_closes_it():
    assert (
        mapping.charge_state(_vehicle(charge_state=9).state, 0)["charging_state"]
        == "Complete"
    )


def test_unplugged_is_disconnected():
    assert (
        mapping.charge_state(_vehicle(charge_state=1).state, 0)["charging_state"]
        == "Disconnected"
    )


def test_cable_connected_but_idle_is_stopped():
    assert (
        mapping.charge_state(_vehicle(charge_state=2).state, 0)["charging_state"]
        == "Stopped"
    )


# ── drive state ───────────────────────────────────────────────────────────


def test_parked_car_reports_null_speed_not_zero():
    """TeslaMate reads a numeric speed as motion; 0.0 while parked would
    look like a stopped car mid-drive rather than a parked one."""
    ds = mapping.drive_state(_vehicle(power=1, speed=0.0).state, 0)
    assert ds["speed"] is None
    assert ds["shift_state"] is None


def test_driving_reports_speed_in_mph():
    ds = mapping.drive_state(_vehicle(power=4, speed=100.0).state, 0)
    assert ds["speed"] == pytest.approx(62.1, abs=0.2)  # 100 km/h
    assert ds["shift_state"] == "D"


def test_gps_is_passed_through_unrounded():
    ds = mapping.drive_state(_vehicle().state, 0)
    assert ds["latitude"] == pytest.approx(37.7749, abs=1e-7)
    assert ds["longitude"] == pytest.approx(-122.4194, abs=1e-7)


# ── shape: what TeslaMate requires structurally ───────────────────────────


def test_products_entry_carries_vehicle_id():
    """TeslaMate filters /api/1/products for entries with a vehicle_id key —
    omit it and the car never appears at all."""
    assert "vehicle_id" in mapping.vehicle_summary(_vehicle(), 42)


def test_vehicle_data_has_every_section_teslamate_parses():
    data = mapping.vehicle_data(_vehicle(), 42)
    for section in (
        "charge_state",
        "climate_state",
        "drive_state",
        "vehicle_state",
        "vehicle_config",
        "gui_settings",
    ):
        assert section in data, f"missing {section}"


def test_doors_use_tesla_encoding():
    """Tesla: 0 = closed, non-zero = open. Lucid: 2 = closed."""
    vs = mapping.vehicle_state(_vehicle().state, _vehicle(), 0)
    assert vs["df"] == 0
    assert vs["locked"] is True


# ── identity: the car is not a Tesla ─────────────────────────────────────────


def test_car_type_is_unrecognised_so_teslamate_shows_no_tesla_model():
    """TeslaMate maps car_type -> model and renders a hardcoded "Model {x}",
    skipping the block when model is nil. v1 of this file sent "models"/"p100d"
    and displayed the owner's Gravity as a "Model S P100D". Anything TeslaMate
    recognises is a regression."""
    cfg = mapping.vehicle_config(0)
    assert cfg["car_type"] not in {
        "models", "models2", "model3", "modelx", "modely", "lychee", "tamarind",
    }
    assert not cfg["car_type"].startswith(("models", "model3", "modelx", "modely"))


def test_no_trim_badging_so_no_phantom_p100d():
    assert mapping.vehicle_config(0)["trim_badging"] is None


# ── transient sensor spikes ───────────────────────────────────────────────


def test_impossible_outside_temp_is_dropped_not_recorded():
    """2026-08-10: the car reported exterior_temp 109.6 C with a 25.8 C cabin,
    and corrected itself two minutes later. TeslaMate charts whatever it is
    given, so one spike ruins the axis for a day."""
    v = _vehicle()
    v.state.cabin.exterior_temp = 109.6
    assert mapping.climate_state(v.state, 0)["outside_temp"] is None


def test_plausible_temps_still_pass_through():
    v = _vehicle()
    v.state.cabin.exterior_temp = 43.0  # a genuinely heat-soaked sensor in sun
    assert mapping.climate_state(v.state, 0)["outside_temp"] == pytest.approx(43.0)
    v.state.cabin.exterior_temp = -20.0
    assert mapping.climate_state(v.state, 0)["outside_temp"] == pytest.approx(-20.0)
