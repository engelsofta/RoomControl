"""Room persistence, validation and slow valve output."""

import asyncio
from copy import deepcopy
import logging
from datetime import datetime, timedelta
from math import isfinite
from uuid import uuid4

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import EXERCISE_STORAGE_KEY, HISTORY_STORAGE_KEY, LEARNING_STORAGE_KEY, MANUAL_STORAGE_KEY, STORAGE_KEY, STORAGE_VERSION
from .control import DEFAULT_HEATING_RATE, DEFAULT_HOLD_VALVES, ControlState, clamp, detect_open_window, heat_gap, hold_valve, learn_heating_rate, learn_hold_valve, next_switches, observed_heating_rate, outdoor_offset, preheat_plan, rolling_mean, schedule_target, stable_for_learning, update_control

_LOGGER = logging.getLogger(__name__)


def target_entity_id(room_id: str) -> str:
    """Stable entity for the calculated target shown in Home Assistant dialogs."""
    return f"sensor.engelsoft_roomcontrol_{room_id.replace('-', '_')}_solltemperatur"


def validate_room(data: dict) -> dict:
    """Return a clean JSON-ready room or raise ValueError."""
    name = str(data.get("name", "")).strip()
    if not 1 <= len(name) <= 60:
        raise ValueError("Raumname muss 1 bis 60 Zeichen haben")
    actual = str(data.get("actual_entity", ""))
    outdoor = str(data.get("outdoor_entity", ""))
    valve = str(data.get("valve_entity", ""))
    if not actual.startswith("sensor."):
        raise ValueError("Ist-Wert muss ein Sensor sein")
    if outdoor and not outdoor.startswith("sensor."):
        raise ValueError("Außentemperatur muss ein Sensor sein")
    if not valve.startswith(("number.", "input_number.")):
        raise ValueError("Ventil-Ziel muss number oder input_number sein")
    if actual == outdoor or actual == valve or outdoor == valve:
        raise ValueError("Bitte unterschiedliche Entitäten wählen")
    schedule = data.get("schedule")
    if not isinstance(schedule, list) or len(schedule) != 7:
        raise ValueError("Wochenplan muss sieben Tage enthalten")
    clean_days = []
    for day in schedule:
        if not isinstance(day, list) or len(day) > 48:
            raise ValueError("Pro Tag sind höchstens 48 Schaltpunkte erlaubt")
        clean = []
        minutes = set()
        for slot in day:
            if not isinstance(slot, dict):
                raise ValueError("Ungültiger Schaltpunkt")
            minute = slot.get("minute")
            temperature = slot.get("temperature")
            if type(minute) is not int or not 0 <= minute <= 1439 or minute in minutes:
                raise ValueError("Schaltzeit muss eindeutig und minutengenau sein")
            if isinstance(temperature, bool) or not isinstance(temperature, (int, float)) or not isfinite(temperature) or not 5 <= temperature <= 35:
                raise ValueError("Solltemperatur muss zwischen 5 und 35 °C liegen")
            minutes.add(minute)
            clean.append({"minute": minute, "temperature": round(float(temperature), 1)})
        clean_days.append(sorted(clean, key=lambda item: item["minute"]))
    if not any(clean_days):
        raise ValueError("Mindestens ein Schaltpunkt ist nötig")
    offsets = {}
    for key, default in (("cold_offset", 1.0), ("warm_offset", -1.0)):
        value = data.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) or not -2 <= value <= 2:
            raise ValueError("Außenkorrektur muss zwischen -2 und +2 °C liegen")
        offsets[key] = round(float(value), 1)
    minimum_valve = data.get("minimum_valve", 0)
    if isinstance(minimum_valve, bool) or not isinstance(minimum_valve, (int, float)) or not isfinite(minimum_valve) or not 0 <= minimum_valve <= 100:
        raise ValueError("Ventil-Mindestöffnung muss zwischen 0 und 100 % liegen")
    return {
        "id": str(data.get("id") or uuid4()),
        "name": name,
        "actual_entity": actual,
        "outdoor_entity": outdoor,
        **offsets,
        "valve_entity": valve,
        "minimum_valve": round(float(minimum_valve), 1),
        "schedule": clean_days,
        "enabled": bool(data.get("enabled", True)),
    }


def numeric_state(hass: HomeAssistant, entity_id: str) -> float | None:
    """Read a finite numeric entity state."""
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None:
        return None
    try:
        value = float(state.state)
    except (TypeError, ValueError):
        return None
    return value if isfinite(value) else None


def valve_state(hass: HomeAssistant, entity_id: str) -> float | None:
    """Allow tiny boundary errors from a valve's reported number state."""
    value = numeric_state(hass, entity_id)
    return normalized_valve(value)


def normalized_valve(value: float | None) -> float | None:
    if value is None or not isfinite(value) or not -0.5 <= value <= 100.5:
        return None
    return clamp(value, 0, 100)


def _is_number(value: object) -> bool:
    try:
        return isfinite(float(value))
    except (TypeError, ValueError):
        return False


def temperature_state(hass: HomeAssistant, entity_id: str) -> float | None:
    """Read a temperature in Celsius, accepting Fahrenheit sensors too."""
    value = numeric_state(hass, entity_id)
    if value is None:
        return None
    state = hass.states.get(entity_id)
    unit = state.attributes.get("unit_of_measurement")
    if unit == "°F":
        return (value - 32) * 5 / 9
    if unit not in (None, "°C"):
        return None
    return value


class RoomManager:
    """Own the saved rooms and their transient control states."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.store = Store[dict](hass, STORAGE_VERSION, STORAGE_KEY)
        self.history_store = Store[dict](hass, 1, HISTORY_STORAGE_KEY)
        self.manual_store = Store[dict](hass, 1, MANUAL_STORAGE_KEY)
        self.learning_store = Store[dict](hass, 1, LEARNING_STORAGE_KEY)
        self.exercise_store = Store[dict](hass, 1, EXERCISE_STORAGE_KEY)
        self.rooms: dict[str, dict] = {}
        self.outdoor_entity = ""
        self.heating_gate_enabled = True
        self.heating_gate_hours = 4.0
        self.heating_gate_on = 16.0
        self.heating_gate_off = 18.0
        self.heating_gate_allowed = True
        self.heating_gate_status: dict = {}
        self.valve_exercise_enabled = True
        self.exercise_last_full: dict[str, datetime] = {}
        self.exercise_last_attempt: dict[str, datetime] = {}
        self.exercise_last_run: dict[str, datetime] = {}
        self.exercise_last_result: dict[str, str] = {}
        self.exercise_active: dict[str, dict] = {}
        self.exercise_full_seen: set[str] = set()
        self.exercise_last_start: datetime | None = None
        self.exercise_dirty = False
        self.outdoor_samples: dict[str, list[tuple[datetime, float]]] = {}
        self.last_history_save: datetime | None = None
        self.manual_dirty = False
        self.learning_dirty = False
        self.last_learning: dict[str, datetime] = {}
        self.learning_models: dict[str, list[float]] = {}
        self.learning_counts: dict[str, int] = {}
        self.heating_rates: dict[str, float] = {}
        self.heating_rate_counts: dict[str, int] = {}
        self.last_rate_learning: dict[str, datetime] = {}
        self.heating_samples: dict[str, list[tuple[datetime, float, float, float]]] = {}
        self.preheat_locks: dict[str, dict] = {}
        self.pending_manual: dict[str, dict] = {}
        self.recent_auto_commands: dict[str, list[tuple[datetime, float]]] = {}
        self.runtime: dict[str, ControlState] = {}
        self.window_states: dict[str, ControlState] = {}
        self.window_alarm_entities: dict[str, object] = {}
        self.add_window_alarm = None
        self.remove_window_alarm = None
        self.status: dict[str, dict] = {}
        self.lock = asyncio.Lock()

    async def load(self) -> None:
        """Load persistent room definitions."""
        saved = await self.store.async_load() or {}
        legacy_outdoor = next((str(raw.get("outdoor_entity", "")) for raw in saved.get("rooms", []) if raw.get("outdoor_entity")), "")
        self.outdoor_entity = str(saved.get("outdoor_entity", legacy_outdoor))
        if self.outdoor_entity and not self.outdoor_entity.startswith("sensor."):
            self.outdoor_entity = ""
        self.heating_gate_enabled = saved.get("heating_gate_enabled") is not False
        for key, default, lower, upper in (
            ("heating_gate_hours", 4.0, 3.0, 6.0),
            ("heating_gate_on", 16.0, -20.0, 35.0),
            ("heating_gate_off", 18.0, -20.0, 35.0),
        ):
            value = saved.get(key, default)
            setattr(self, key, float(value) if type(value) in (int, float) and isfinite(value) and lower <= value <= upper else default)
        if self.heating_gate_off < self.heating_gate_on + 0.5:
            self.heating_gate_on, self.heating_gate_off = 16.0, 18.0
        self.heating_gate_allowed = saved.get("heating_gate_allowed") is not False
        self.valve_exercise_enabled = saved.get("valve_exercise_enabled") is not False
        for raw in saved.get("rooms", []):
            try:
                room = validate_room({**raw, "outdoor_entity": self.outdoor_entity})
            except ValueError as err:
                _LOGGER.warning("Skipping invalid saved room: %s", err)
                continue
            self.rooms[room["id"]] = room
        exercise_saved = await self.exercise_store.async_load() or {}
        now = dt_util.now()
        for field, destination in (
            ("last_full", self.exercise_last_full),
            ("last_attempt", self.exercise_last_attempt),
            ("last_run", self.exercise_last_run),
        ):
            for room_id, raw in exercise_saved.get(field, {}).items():
                if room_id not in self.rooms:
                    continue
                try:
                    stamp = datetime.fromisoformat(raw)
                    if stamp.tzinfo is not None and stamp <= now:
                        destination[room_id] = stamp
                except (TypeError, ValueError):
                    pass
        self.exercise_last_result = {
            room_id: value for room_id, value in exercise_saved.get("last_result", {}).items()
            if room_id in self.rooms and value in ("done", "unconfirmed", "interrupted", "failed", "manual")
        }
        try:
            stamp = datetime.fromisoformat(exercise_saved["last_start"])
            if stamp.tzinfo is not None and stamp <= now:
                self.exercise_last_start = stamp
        except (KeyError, TypeError, ValueError):
            pass
        for room_id, raw in exercise_saved.get("active", {}).items():
            if room_id not in self.rooms or not isinstance(raw, dict):
                continue
            try:
                started = datetime.fromisoformat(raw["started"])
                restore = float(raw["restore"])
                if started.tzinfo is not None and started <= now and isfinite(restore) and 0 <= restore <= 100:
                    self.exercise_active[room_id] = {"started": started, "until": started + timedelta(minutes=10), "restore": restore, "interrupted": True}
            except (KeyError, TypeError, ValueError):
                pass
        for room_id in self.rooms:
            if room_id not in self.exercise_last_full:
                self.exercise_last_full[room_id] = now
                self.exercise_dirty = True
        history = await self.history_store.async_load() or {}
        for entity_id, raw_samples in history.get("samples", {}).items():
            if not entity_id.startswith("sensor.") or not isinstance(raw_samples, list):
                continue
            samples = []
            for raw in raw_samples:
                try:
                    stamp = datetime.fromisoformat(raw[0])
                    value = float(raw[1])
                    if stamp.tzinfo is not None and now - timedelta(hours=24) <= stamp <= now and isfinite(value):
                        samples.append((stamp, value))
                except (ValueError, TypeError, IndexError):
                    continue
            self.outdoor_samples[entity_id] = sorted(samples)
        saved_manual = await self.manual_store.async_load() or {}
        for room_id, raw in saved_manual.get("rooms", {}).items():
            if room_id not in self.rooms:
                continue
            try:
                until = datetime.fromisoformat(raw["until"])
                valve = float(raw["value"])
            except (KeyError, TypeError, ValueError):
                continue
            if until.tzinfo is not None and until > now and isfinite(valve) and 0 <= valve <= 100:
                self.runtime[room_id] = ControlState(
                    valve=valve, manual_until=until, manual_value=valve
                )
        saved_learning = await self.learning_store.async_load() or {}
        for room_id, raw in saved_learning.get("rooms", {}).items():
            if room_id not in self.rooms or not isinstance(raw, dict):
                continue
            values = raw.get("valves")
            if isinstance(values, list) and len(values) == 4 and all(
                isinstance(value, (int, float)) and not isinstance(value, bool)
                and isfinite(value) and 5 <= value <= 90 for value in values
            ):
                self.learning_models[room_id] = [float(value) for value in values]
                try:
                    self.learning_counts[room_id] = max(0, int(raw.get("count", 0)))
                except (TypeError, ValueError):
                    self.learning_counts[room_id] = 0
                try:
                    last = datetime.fromisoformat(raw["last_learning"])
                    if last.tzinfo is not None and last <= now:
                        self.last_learning[room_id] = last
                except (KeyError, TypeError, ValueError):
                    pass
                rate = raw.get("heating_rate")
                if isinstance(rate, (int, float)) and not isinstance(rate, bool) and isfinite(rate) and 0.05 <= rate <= 1.5:
                    self.heating_rates[room_id] = float(rate)
                    try:
                        self.heating_rate_counts[room_id] = max(0, int(raw.get("rate_count", 0)))
                    except (TypeError, ValueError):
                        self.heating_rate_counts[room_id] = 0
                    try:
                        last_rate = datetime.fromisoformat(raw["last_rate_learning"])
                        if last_rate.tzinfo is not None and last_rate <= now:
                            self.last_rate_learning[room_id] = last_rate
                    except (KeyError, TypeError, ValueError):
                        pass
                locked = raw.get("preheat_lock")
                if isinstance(locked, dict):
                    try:
                        switch = datetime.fromisoformat(locked["time"])
                        lead = float(locked["lead_hours"])
                        if switch.tzinfo is not None and now < switch <= now + timedelta(hours=8) and isfinite(lead) and 0 <= lead <= 8:
                            self.preheat_locks[room_id] = {"time": locked["time"], "lead_hours": lead}
                    except (KeyError, TypeError, ValueError):
                        pass

    async def _save_rooms(self) -> None:
        await self.store.async_save({
            "outdoor_entity": self.outdoor_entity,
            "heating_gate_enabled": self.heating_gate_enabled,
            "heating_gate_hours": self.heating_gate_hours,
            "heating_gate_on": self.heating_gate_on,
            "heating_gate_off": self.heating_gate_off,
            "heating_gate_allowed": self.heating_gate_allowed,
            "valve_exercise_enabled": self.valve_exercise_enabled,
            "rooms": list(self.rooms.values()),
        })

    async def save_settings(self, data: dict) -> None:
        """Apply the shared outdoor sensor and heating gate to every room."""
        entity = data.get("outdoor_entity", "")
        if not isinstance(entity, str) or (entity and not entity.startswith("sensor.")):
            raise ValueError("Außentemperatur muss ein Sensor sein")
        enabled = data.get("heating_gate_enabled", self.heating_gate_enabled)
        if type(enabled) is not bool:
            raise ValueError("Heizfreigabe muss ein Schalter sein")
        exercise_enabled = data.get("valve_exercise_enabled", self.valve_exercise_enabled)
        if type(exercise_enabled) is not bool:
            raise ValueError("Ventilschutz muss ein Schalter sein")
        values = {}
        for key, default, lower, upper in (
            ("heating_gate_hours", self.heating_gate_hours, 3.0, 6.0),
            ("heating_gate_on", self.heating_gate_on, -20.0, 35.0),
            ("heating_gate_off", self.heating_gate_off, -20.0, 35.0),
        ):
            value = data.get(key, default)
            if type(value) not in (int, float) or not isfinite(value) or not lower <= value <= upper:
                raise ValueError("Ungültiger Wert für die Heizfreigabe")
            values[key] = round(float(value), 1)
        if values["heating_gate_off"] < values["heating_gate_on"] + 0.5:
            raise ValueError("Ausschalttemperatur muss mindestens 0,5 °C über der Einschalttemperatur liegen")
        async with self.lock:
            changed = (entity != self.outdoor_entity or enabled != self.heating_gate_enabled or
                       any(getattr(self, key) != value for key, value in values.items()))
            self.outdoor_entity = entity
            self.heating_gate_enabled = enabled
            self.valve_exercise_enabled = exercise_enabled
            for key, value in values.items():
                setattr(self, key, value)
            if changed:
                self.heating_gate_allowed = True
            for room in self.rooms.values():
                room["outdoor_entity"] = entity
            await self._save_rooms()
        await self.tick()
        await self.exercise_tick()

    async def save_room(self, data: dict) -> dict:
        """Persist a complete room definition."""
        room = validate_room({**data, "outdoor_entity": self.outdoor_entity})
        if any(
            other["valve_entity"] == room["valve_entity"] and other_id != room["id"]
            for other_id, other in self.rooms.items()
        ):
            raise ValueError("Dieses Ventil-Ziel wird bereits in einem anderen Raum geregelt")
        existing = self.rooms.get(room["id"])
        if existing and any(existing[key] != room[key] for key in ("valve_entity", "actual_entity")):
            self.runtime.pop(room["id"], None)
            self.window_states.pop(room["id"], None)
            self.manual_dirty = True
            self.learning_models.pop(room["id"], None)
            self.learning_counts.pop(room["id"], None)
            self.last_learning.pop(room["id"], None)
            self.heating_rates.pop(room["id"], None)
            self.heating_rate_counts.pop(room["id"], None)
            self.last_rate_learning.pop(room["id"], None)
            self.preheat_locks.pop(room["id"], None)
            self.learning_dirty = True
        if existing and existing["schedule"] != room["schedule"]:
            self.preheat_locks.pop(room["id"], None)
            self.learning_dirty = True
        self.heating_samples.pop(room["id"], None)
        self.pending_manual.pop(room["id"], None)
        async with self.lock:
            if existing and existing["valve_entity"] != room["valve_entity"]:
                active = self.exercise_active.get(room["id"])
                if active:
                    restore = 0 if not self.heating_gate_allowed else int(active["restore"] + 0.5)
                    if not await self._write_valve(existing, dt_util.now(), restore):
                        raise ValueError("Ventilschutzfahrt am bisherigen Ventil konnte nicht beendet werden")
                    self.exercise_active.pop(room["id"])
                self.exercise_last_full[room["id"]] = dt_util.now()
                self.exercise_last_attempt.pop(room["id"], None)
                self.exercise_last_run.pop(room["id"], None)
                self.exercise_last_result.pop(room["id"], None)
                self.exercise_full_seen.discard(room["id"])
                self.exercise_dirty = True
            self.rooms[room["id"]] = room
            if room["id"] not in self.exercise_last_full:
                self.exercise_last_full[room["id"]] = dt_util.now()
                self.exercise_dirty = True
            await self._save_rooms()
        await self.tick()
        await self.exercise_tick()
        if existing is None and self.add_window_alarm is not None:
            self.add_window_alarm(room)
        return room

    async def delete_room(self, room_id: str) -> None:
        """Delete one room without changing its former valve state."""
        async with self.lock:
            if room_id not in self.rooms:
                raise ValueError("Raum nicht gefunden")
            active = self.exercise_active.get(room_id)
            if active:
                room = self.rooms[room_id]
                restore = 0 if not self.heating_gate_allowed else int(active["restore"] + 0.5)
                if not await self._write_valve(room, dt_util.now(), restore):
                    raise ValueError("Ventilschutzfahrt konnte vor dem Löschen nicht beendet werden")
            if self.remove_window_alarm is not None:
                await self.remove_window_alarm(room_id)
            self.rooms.pop(room_id)
            self.hass.states.async_remove(target_entity_id(room_id))
            self.runtime.pop(room_id, None)
            self.window_states.pop(room_id, None)
            self.status.pop(room_id, None)
            self.learning_models.pop(room_id, None)
            self.learning_counts.pop(room_id, None)
            self.last_learning.pop(room_id, None)
            self.heating_rates.pop(room_id, None)
            self.heating_rate_counts.pop(room_id, None)
            self.last_rate_learning.pop(room_id, None)
            self.heating_samples.pop(room_id, None)
            self.preheat_locks.pop(room_id, None)
            self.pending_manual.pop(room_id, None)
            self.recent_auto_commands.pop(room_id, None)
            self.exercise_last_full.pop(room_id, None)
            self.exercise_last_attempt.pop(room_id, None)
            self.exercise_last_run.pop(room_id, None)
            self.exercise_last_result.pop(room_id, None)
            self.exercise_active.pop(room_id, None)
            self.exercise_full_seen.discard(room_id)
            self.exercise_dirty = True
            self.learning_dirty = True
            await self._save_rooms()
            self.manual_dirty = True
            await self._save_manual()
            await self._save_learning()
            await self._save_exercise()

    async def reorder_rooms(self, room_ids: list[str]) -> None:
        """Persist the exact order chosen in the room overview."""
        async with self.lock:
            if (
                not isinstance(room_ids, list)
                or len(room_ids) != len(self.rooms)
                or any(not isinstance(room_id, str) for room_id in room_ids)
                or set(room_ids) != set(self.rooms)
            ):
                raise ValueError("Die Raumreihenfolge ist ungültig")
            self.rooms = {room_id: self.rooms[room_id] for room_id in room_ids}
            await self._save_rooms()

    async def _save_learning(self) -> None:
        if not self.learning_dirty:
            return
        await self.learning_store.async_save({"rooms": {
            room_id: {
                "valves": model,
                "count": self.learning_counts.get(room_id, 0),
                "last_learning": self.last_learning[room_id].isoformat() if room_id in self.last_learning else None,
                "heating_rate": self.heating_rates.get(room_id, DEFAULT_HEATING_RATE),
                "rate_count": self.heating_rate_counts.get(room_id, 0),
                "last_rate_learning": self.last_rate_learning[room_id].isoformat() if room_id in self.last_rate_learning else None,
                "preheat_lock": self.preheat_locks.get(room_id),
            }
            for room_id, model in self.learning_models.items() if room_id in self.rooms
        }})
        self.learning_dirty = False

    async def _save_manual(self) -> None:
        await self.manual_store.async_save({"rooms": {
            room_id: {"until": state.manual_until.isoformat(), "value": state.valve}
            for room_id, state in self.runtime.items()
            if state.manual_until is not None and room_id in self.rooms
        }})
        self.manual_dirty = False

    async def _save_exercise(self) -> None:
        if not self.exercise_dirty:
            return
        await self.exercise_store.async_save({
            "last_full": {room_id: stamp.isoformat() for room_id, stamp in self.exercise_last_full.items() if room_id in self.rooms},
            "last_attempt": {room_id: stamp.isoformat() for room_id, stamp in self.exercise_last_attempt.items() if room_id in self.rooms},
            "last_run": {room_id: stamp.isoformat() for room_id, stamp in self.exercise_last_run.items() if room_id in self.rooms},
            "last_result": {room_id: value for room_id, value in self.exercise_last_result.items() if room_id in self.rooms},
            "last_start": self.exercise_last_start.isoformat() if self.exercise_last_start else None,
            "active": {room_id: {"started": item["started"].isoformat(), "restore": item["restore"]}
                       for room_id, item in self.exercise_active.items() if room_id in self.rooms},
        })
        self.exercise_dirty = False

    async def prepare_manual_edit(self, room_id: str) -> bool:
        """Watch briefly for a real change made in the HA number dialog."""
        async with self.lock:
            room = self.rooms.get(room_id)
            if room is None:
                raise ValueError("Raum nicht gefunden")
            if not room["enabled"]:
                return False
            current = valve_state(self.hass, room["valve_entity"])
            if current is not None:
                self.pending_manual[room_id] = {"until": dt_util.now() + timedelta(minutes=10), "initial": current}
        return current is not None

    async def stop_manual_hold(self, room_id: str) -> bool:
        """Resume automatic regulation immediately when the badge is clicked."""
        async with self.lock:
            if room_id not in self.rooms:
                raise ValueError("Raum nicht gefunden")
            self.pending_manual.pop(room_id, None)
            control = self.runtime.get(room_id)
            if control is None or control.manual_until is None:
                return False
            control.manual_until = None
            control.manual_value = None
            self.manual_dirty = True
        await self.tick()
        return True

    def snapshot(self) -> dict:
        """Provide panel data without exposing internal state objects."""
        return {
            "rooms": list(self.rooms.values()), "status": self.status,
            "settings": {
                "outdoor_entity": self.outdoor_entity,
                "heating_gate_enabled": self.heating_gate_enabled,
                "heating_gate_hours": self.heating_gate_hours,
                "heating_gate_on": self.heating_gate_on,
                "heating_gate_off": self.heating_gate_off,
                "valve_exercise_enabled": self.valve_exercise_enabled,
            },
            "heating_gate": self.heating_gate_status,
            "valve_exercise": self._exercise_snapshot(dt_util.now()),
        }

    def _exercise_snapshot(self, now: datetime) -> dict[str, dict]:
        """Show each room's maintenance state and next eligible start."""
        result = {}
        slot = max(now, self.exercise_last_start + timedelta(minutes=2)) if self.exercise_last_start else now
        for room_id, room in self.rooms.items():
            active = self.exercise_active.get(room_id)
            state = "running" if active else "scheduled"
            next_run = None
            if active:
                state = "restore_error" if active.get("restore_error") else "running"
            elif not self.valve_exercise_enabled:
                state = "disabled"
            elif not room["enabled"]:
                state = "room_disabled"
            else:
                last_full = self.exercise_last_full.get(room_id, now)
                current = valve_state(self.hass, room["valve_entity"])
                if current is None:
                    state = "unavailable"
                else:
                    due = (now if current >= 99.5 else last_full) + timedelta(days=7)
                    attempt = self.exercise_last_attempt.get(room_id)
                    if attempt:
                        due = max(due, attempt + timedelta(days=1))
                    control = self.runtime.get(room_id)
                    if control and control.manual_until and control.manual_until > due:
                        due = control.manual_until
                    if due <= slot:
                        next_run = slot
                        slot += timedelta(minutes=2)
                        state = "due"
                    else:
                        next_run = due
            result[room_id] = {
                "state": state,
                "next_run": next_run.isoformat() if next_run else None,
                "until": active["until"].isoformat() if active else None,
                "last_full": self.exercise_last_full[room_id].isoformat() if room_id in self.exercise_last_full else None,
                "last_run": self.exercise_last_run[room_id].isoformat() if room_id in self.exercise_last_run else None,
                "last_result": self.exercise_last_result.get(room_id),
            }
        return result

    async def fresh_snapshot(self) -> dict:
        """Recheck an error when the panel asks for current values."""
        if any(item.get("state") == "error" for item in self.status.values()):
            await self.tick()
        return self.snapshot()

    @callback
    def handle_state_change(self, event) -> None:
        """Refresh newly ready entities and detect actual manual valve changes."""
        entity_id = event.data.get("entity_id")
        state = event.data.get("new_state")
        if state is None:
            return
        old_state = event.data.get("old_state")
        for room in self.rooms.values():
            if room["valve_entity"] != entity_id:
                continue
            old_value = normalized_valve(float(old_state.state)) if old_state and _is_number(old_state.state) else None
            new_value = normalized_valve(float(state.state)) if _is_number(state.state) else None
            if old_value is not None and old_value >= 99.5 and (new_value is None or new_value < 99.5):
                self.exercise_last_full[room["id"]] = dt_util.now()
                self.exercise_dirty = True
            elif new_value is not None and new_value >= 99.5 and (old_value is None or old_value < 99.5):
                self.exercise_last_full[room["id"]] = dt_util.now()
                self.exercise_dirty = True
            break
        if (
            any(entity_id in (room["actual_entity"], room["outdoor_entity"], room["valve_entity"]) for room in self.rooms.values())
            and (old_state is None or old_state.state in ("unknown", "unavailable"))
        ):
            try:
                ready = isfinite(float(state.state))
            except (TypeError, ValueError):
                ready = False
            if ready:
                self.hass.async_create_background_task(self.tick(), "room_thermostat_entity_ready")
        try:
            value = normalized_valve(float(state.state))
        except (TypeError, ValueError):
            return
        if value is None:
            return
        try:
            old_value = normalized_valve(float(old_state.state)) if old_state is not None else None
        except (TypeError, ValueError):
            old_value = None
        now = dt_util.now()
        for room in self.rooms.values():
            if room["valve_entity"] != entity_id or not room["enabled"]:
                continue
            room_id = room["id"]
            pending = self.pending_manual.get(room_id)
            if pending and now > pending["until"]:
                self.pending_manual.pop(room_id, None)
                pending = None
            previous = old_value if old_value is not None else pending["initial"] if pending else None
            if previous is not None and abs(value - previous) < 0.05:
                return
            user_changed = bool(state.context.user_id)
            if not user_changed:
                if pending is None:
                    return
                recent = [command for command in self.recent_auto_commands.get(room_id, []) if now - command[0] <= timedelta(minutes=15)]
                self.recent_auto_commands[room_id] = recent
                if any(abs(value - command[1]) < 0.05 for command in recent):
                    return
            control = self.runtime.setdefault(room["id"], ControlState())
            control.manual_value = value
            control.manual_until = now + timedelta(hours=2)
            control.valve = value
            control.integral = 0
            self.manual_dirty = True
            self.pending_manual.pop(room_id, None)
            self.heating_samples.pop(room_id, None)
            self.hass.async_create_background_task(self.tick(), "room_thermostat_manual_refresh")
            break

    def _record_outdoor(self, now: datetime) -> None:
        active = {room["outdoor_entity"] for room in self.rooms.values() if room["outdoor_entity"]}
        for old_entity in set(self.outdoor_samples) - active:
            self.outdoor_samples.pop(old_entity, None)
        for entity_id in active:
            value = temperature_state(self.hass, entity_id)
            samples = self.outdoor_samples.setdefault(entity_id, [])
            samples[:] = [(stamp, item) for stamp, item in samples if stamp >= now - timedelta(hours=24)]
            if value is not None and (not samples or now - samples[-1][0] >= timedelta(minutes=4)):
                samples.append((now, value))

    def _observe_full_open(self, room_id: str, current: float | None, now: datetime) -> None:
        if current is not None and current >= 99.5:
            if room_id not in self.exercise_full_seen:
                self.exercise_full_seen.add(room_id)
                self.exercise_last_full[room_id] = now
                self.exercise_dirty = True
        elif room_id in self.exercise_full_seen:
            self.exercise_full_seen.discard(room_id)
            self.exercise_last_full[room_id] = now
            self.exercise_dirty = True

    async def exercise_tick(self) -> None:
        """Run overdue valve protection with two minutes between room starts."""
        recalculate = False
        async with self.lock:
            now = dt_util.now()
            for room_id, active in list(self.exercise_active.items()):
                room = self.rooms.get(room_id)
                control = self.runtime.get(room_id)
                manual = bool(control and control.manual_until and control.manual_until > now)
                current = valve_state(self.hass, room["valve_entity"]) if room else None
                if room:
                    self._observe_full_open(room_id, current, now)
                if manual:
                    self.exercise_active.pop(room_id)
                    self.exercise_last_result[room_id] = "manual"
                    self.exercise_dirty = True
                    recalculate = True
                    continue
                if now < active["until"] and not active.get("interrupted") and room and room["enabled"] and self.valve_exercise_enabled:
                    continue
                restore = 0 if not room or not room["enabled"] or not self.heating_gate_allowed else int(active["restore"] + 0.5)
                if room and (current is None or abs(current - restore) >= 0.5):
                    if not await self._write_valve(room, now, restore):
                        active["restore_error"] = True
                        continue
                self.exercise_active.pop(room_id)
                self.exercise_last_result[room_id] = (
                    "interrupted" if active.get("interrupted") else
                    "done" if current is not None and current >= 99.5 and now >= active["until"] else "unconfirmed"
                )
                if self.exercise_last_result[room_id] == "done":
                    self.exercise_last_full[room_id] = now
                    self.exercise_last_run[room_id] = now
                self.exercise_dirty = True
                recalculate = True

            if self.valve_exercise_enabled and not any(item.get("restore_error") for item in self.exercise_active.values()):
                next_slot = self.exercise_last_start + timedelta(minutes=2) if self.exercise_last_start else now
                if now >= next_slot:
                    for room_id, room in self.rooms.items():
                        if not room["enabled"] or room_id in self.exercise_active:
                            continue
                        target_state = self.hass.states.get(room["valve_entity"])
                        if target_state is None:
                            continue
                        try:
                            if float(target_state.attributes.get("min", 0)) > 0 or float(target_state.attributes.get("max", 100)) < 100:
                                continue
                        except (TypeError, ValueError):
                            continue
                        current = valve_state(self.hass, room["valve_entity"])
                        self._observe_full_open(room_id, current, now)
                        if current is None or current >= 99.5:
                            continue
                        control = self.runtime.get(room_id)
                        if control and control.manual_until and control.manual_until > now:
                            continue
                        last_full = self.exercise_last_full.get(room_id, now)
                        last_attempt = self.exercise_last_attempt.get(room_id)
                        if now - last_full < timedelta(days=7) or (last_attempt and now - last_attempt < timedelta(days=1)):
                            continue
                        self.exercise_last_attempt[room_id] = now
                        self.exercise_last_start = now
                        self.exercise_dirty = True
                        previous = self.status.get(room_id, {}).get("valve")
                        restore = previous if isinstance(previous, (int, float)) and isfinite(previous) else current
                        self.exercise_active[room_id] = {
                            "started": now, "until": now + timedelta(minutes=10), "restore": clamp(restore, 0, 100),
                        }
                        await self._save_exercise()
                        if await self._write_valve(room, now, 100):
                            self.exercise_last_result.pop(room_id, None)
                            if room_id in self.status:
                                self.status[room_id].update(state="exercise", message="Ventilschutzfahrt läuft", valve=100, valve_30m=None)
                        else:
                            self.exercise_active.pop(room_id)
                            self.exercise_last_result[room_id] = "failed"
                        self.exercise_dirty = True
                        break
            await self._save_exercise()
        if recalculate:
            await self.tick()

    async def stop_exercises(self) -> bool:
        """Restore valves before an orderly integration unload."""
        async with self.lock:
            now = dt_util.now()
            for room_id, active in list(self.exercise_active.items()):
                room = self.rooms.get(room_id)
                if room is None:
                    continue
                restore = 0 if not room["enabled"] or not self.heating_gate_allowed else int(active["restore"] + 0.5)
                if not await self._write_valve(room, now, restore):
                    active["restore_error"] = True
                    await self._save_exercise()
                    return False
                self.exercise_active.pop(room_id)
                self.exercise_last_result[room_id] = "interrupted"
                self.exercise_dirty = True
            await self._save_exercise()
        return True

    def _update_heating_gate(self, now: datetime) -> bool:
        """Use a shorter outdoor mean and hysteresis for the shared heating permit."""
        samples = self.outdoor_samples.get(self.outdoor_entity, [])
        mean, coverage = rolling_mean(samples, now, self.heating_gate_hours)
        recent_count = sum(now - timedelta(hours=self.heating_gate_hours) <= stamp <= now for stamp, _ in samples)
        current = temperature_state(self.hass, self.outdoor_entity)
        ready = (current is not None and mean is not None and
                 coverage >= self.heating_gate_hours - 0.25 and
                 recent_count >= self.heating_gate_hours * 9)
        previous = self.heating_gate_allowed
        if not self.heating_gate_enabled:
            self.heating_gate_allowed = True
            reason = "disabled"
        elif not ready:
            self.heating_gate_allowed = True
            reason = "unavailable"
        else:
            if self.heating_gate_allowed and mean >= self.heating_gate_off:
                self.heating_gate_allowed = False
            elif not self.heating_gate_allowed and mean <= self.heating_gate_on:
                self.heating_gate_allowed = True
            reason = "allowed" if self.heating_gate_allowed else "blocked"
        self.heating_gate_status = {
            "mean": mean, "coverage": coverage, "allowed": self.heating_gate_allowed,
            "reason": reason, "ready": ready,
        }
        return previous != self.heating_gate_allowed

    async def _save_outdoor_history(self, now: datetime) -> None:
        if self.last_history_save is not None and now - self.last_history_save < timedelta(minutes=30):
            return
        await self.history_store.async_save({"samples": {
            entity_id: [[stamp.isoformat(), value] for stamp, value in samples]
            for entity_id, samples in self.outdoor_samples.items()
        }})
        self.last_history_save = now

    async def tick(self) -> None:
        """Calculate all rooms and write changed valve percentages."""
        async with self.lock:
            now = dt_util.now()
            self.pending_manual = {room_id: pending for room_id, pending in self.pending_manual.items() if now <= pending["until"]}
            self._record_outdoor(now)
            gate_changed = self._update_heating_gate(now)
            for room in list(self.rooms.values()):
                await self._tick_room(room, now)
            if gate_changed:
                await self._save_rooms()
            for sensor in list(self.window_alarm_entities.values()):
                sensor.refresh()
            await self._save_outdoor_history(now)
            if self.manual_dirty:
                await self._save_manual()
            await self._save_learning()
            await self._save_exercise()

    async def _write_valve(self, room: dict, now: datetime, value: int) -> bool:
        """Write and track an automatic valve command for manual-change detection."""
        room_id = room["id"]
        commands = self.recent_auto_commands.setdefault(room_id, [])
        commands[:] = [command for command in commands if now - command[0] <= timedelta(minutes=15)]
        command = (now, value)
        commands.append(command)
        try:
            await self.hass.services.async_call(
                room["valve_entity"].split(".", 1)[0],
                "set_value",
                {"entity_id": room["valve_entity"], "value": value},
                blocking=True,
            )
        except Exception:
            commands.remove(command)
            _LOGGER.exception("Failed to write valve for room %s", room["name"])
            return False
        return True

    def _observe_heating(self, room_id: str, now: datetime, actual: float, target: float, valve: float, baseline: float) -> None:
        """Learn the room's net rise during sustained automatic heating."""
        if target - actual < 0.2 or valve < baseline + 8:
            self.heating_samples.pop(room_id, None)
            return
        samples = self.heating_samples.setdefault(room_id, [])
        samples[:] = [sample for sample in samples if now - sample[0] <= timedelta(hours=3)]
        if samples and now - samples[-1][0] < timedelta(minutes=4):
            return
        samples.append((now, actual, valve, baseline))
        observed = observed_heating_rate(samples, now)
        if observed is None or (
            room_id in self.last_rate_learning and now - self.last_rate_learning[room_id] < timedelta(hours=6)
        ):
            return
        previous = self.heating_rates.get(room_id, DEFAULT_HEATING_RATE)
        self.heating_rates[room_id] = learn_heating_rate(previous, observed)
        self.heating_rate_counts[room_id] = self.heating_rate_counts.get(room_id, 0) + 1
        self.last_rate_learning[room_id] = now
        self.learning_dirty = True

    async def _tick_room(self, room: dict, now: datetime) -> None:
        room_id = room["id"]
        actual = temperature_state(self.hass, room["actual_entity"])
        outdoor = temperature_state(self.hass, room["outdoor_entity"])
        window_open = False
        if actual is not None:
            window_open, _ = detect_open_window(self.window_states.setdefault(room_id, ControlState()), actual, outdoor, now)
        mean, coverage = rolling_mean(self.outdoor_samples.get(room["outdoor_entity"], []), now)
        base = schedule_target(room["schedule"], now)
        offset = outdoor_offset(mean, room["cold_offset"], room["warm_offset"])
        scheduled_target = round(clamp(base + offset, 5, 35), 1)
        rate = self.heating_rates.get(room_id, DEFAULT_HEATING_RATE)
        preheat = preheat_plan(room["schedule"], now, actual, offset, rate, self.preheat_locks.get(room_id)) if room["enabled"] else {"active": False, "lead_hours": None, "time": None, "target": None}
        if preheat["active"] and room["enabled"]:
            lock = {"time": preheat["time"], "lead_hours": preheat["lead_hours"]}
            if self.preheat_locks.get(room_id) != lock:
                self.preheat_locks[room_id] = lock
                self.learning_dirty = True
        elif room_id in self.preheat_locks:
            self.preheat_locks.pop(room_id)
            self.learning_dirty = True
        target = preheat["target"] if preheat["active"] else scheduled_target
        gap = heat_gap(target, mean)
        model = self.learning_models.setdefault(room_id, list(DEFAULT_HOLD_VALVES))
        baseline = hold_valve(model, gap)
        result = {
            "base": base,
            "offset": offset,
            "target": target,
            "target_entity": target_entity_id(room_id),
            "scheduled_target": scheduled_target,
            "actual": actual,
            "outdoor": outdoor,
            "outdoor_mean": mean,
            "outdoor_coverage": coverage,
            "window_available": actual is not None and outdoor is not None,
            "window_open": window_open,
            "valve": None,
            "valve_current": valve_state(self.hass, room["valve_entity"]),
            "valve_30m": None,
            "next_switches": next_switches(room["schedule"], now),
            "filtered": None,
            "hold_valve": baseline,
            "minimum_valve": room["minimum_valve"],
            "learning_count": self.learning_counts.get(room_id, 0),
            "heating_rate": rate,
            "heating_rate_count": self.heating_rate_counts.get(room_id, 0),
            "preheat": preheat,
            "state": "paused",
            "message": "Raum deaktiviert",
        }
        self.status[room_id] = result
        target_state = self.hass.states.get(result["target_entity"])
        target_name = f"{room['name']} Solltemperatur"
        if target_state is None or target_state.state != str(target) or target_state.attributes.get("friendly_name") != target_name:
            self.hass.states.async_set(result["target_entity"], target, {
                "friendly_name": target_name,
                "unit_of_measurement": "°C",
                "device_class": "temperature",
                "state_class": "measurement",
                "icon": "mdi:thermometer-check",
            })
        if not room["enabled"]:
            self.heating_samples.pop(room_id, None)
            if self.runtime.pop(room_id, None) is not None:
                self.manual_dirty = True
            target_state = self.hass.states.get(room["valve_entity"])
            if target_state is None or target_state.state in ("unknown", "unavailable"):
                result.update(state="error", message="Raum deaktiviert, Ventil-Ziel nicht verfügbar")
                return
            try:
                if float(target_state.attributes.get("min", 0)) > 0:
                    raise ValueError
            except (TypeError, ValueError):
                result.update(state="error", message="Raum deaktiviert, Ventil-Ziel unterstützt 0 % nicht")
                return
            current = valve_state(self.hass, room["valve_entity"])
            if current is None:
                result.update(state="error", message="Raum deaktiviert, Ventil-Wert nicht verfügbar")
                return
            if current != 0 and not await self._write_valve(room, now, 0):
                result.update(state="error", message="Raum deaktiviert, Ventil konnte nicht geschlossen werden")
            return
        if actual is None:
            self.heating_samples.pop(room_id, None)
            result.update(state="error", message="Ist-Wert nicht verfügbar")
            return
        target_state = self.hass.states.get(room["valve_entity"])
        if target_state is None or target_state.state in ("unknown", "unavailable"):
            self.heating_samples.pop(room_id, None)
            result.update(state="error", message="Ventil-Ziel nicht verfügbar")
            return
        attributes = target_state.attributes
        minimum = attributes.get("min", 0)
        maximum = attributes.get("max", 100)
        try:
            if float(minimum) > 0 or float(maximum) < 100:
                raise ValueError
        except (TypeError, ValueError):
            result.update(state="error", message="Ventil-Ziel muss 0–100 % unterstützen")
            return
        current = valve_state(self.hass, room["valve_entity"])
        if current is None:
            self.heating_samples.pop(room_id, None)
            result.update(state="error", message="Ventil-Wert nicht verfügbar oder außerhalb 0–100 %")
            return
        self._observe_full_open(room_id, current, now)
        active = self.exercise_active.get(room_id)
        if active:
            control = self.runtime.get(room_id)
            if control and control.manual_until and control.manual_until > now:
                self.exercise_active.pop(room_id)
                self.exercise_last_result[room_id] = "manual"
                self.exercise_dirty = True
            else:
                self.heating_samples.pop(room_id, None)
                result.update(state="exercise", message="Ventilschutzfahrt läuft", valve=100, valve_30m=None)
                return
        if not self.heating_gate_allowed:
            self.heating_samples.pop(room_id, None)
            self.pending_manual.pop(room_id, None)
            if self.runtime.pop(room_id, None) is not None:
                self.manual_dirty = True
            result.update(state="blocked", message="Heizfreigabe wegen Außentemperatur gesperrt", valve=0, valve_30m=0)
            if current != 0 and not await self._write_valve(room, now, 0):
                result.update(state="error", message="Heizfreigabe gesperrt, Ventil konnte nicht geschlossen werden")
            return
        if room_id not in self.runtime:
            # Continue from the physical command after a restart, without a
            # sudden reset to zero or a fictitious integral history.
            initial = clamp(current if current is not None else 0, 0, 100)
            self.runtime[room_id] = ControlState(integral=initial - baseline if initial > 0 else 0, valve=initial)
        control = self.runtime[room_id]
        if control.manual_until and now >= control.manual_until:
            control.manual_until = None
            control.manual_value = None
            self.manual_dirty = True
        if control.manual_until and now < control.manual_until:
            self.heating_samples.pop(room_id, None)
            try:
                update_control(control, actual, target, now, baseline, gap, room["minimum_valve"])
            except ValueError:
                result.update(state="error", message="Ist-Wert außerhalb des gültigen Bereichs")
                return
            control.valve = current if current is not None else control.manual_value
            control.integral = control.valve - baseline
            control.stable_samples = []
            control.cooling_samples = []
            control.last_switch_at = None
            result.update(
                valve=control.valve,
                valve_30m=control.valve,
                filtered=round(control.filtered, 2),
                state="manual",
                manual_until=control.manual_until.isoformat(),
                message="Ventil manuell eingestellt – Automatik pausiert",
            )
            return
        try:
            _, valve = update_control(control, actual, target, now, baseline, gap, room["minimum_valve"])
        except ValueError:
            result.update(state="error", message="Ist-Wert außerhalb des gültigen Bereichs")
            return
        # Keep the controller's fractional value internally, but command whole percent.
        valve_output = int(valve + 0.5)
        result.update(
            valve=valve_output,
            filtered=round(control.filtered, 2),
            state="active",
            message="Vorausschauendes Aufheizen" if preheat["active"] else "Regelung aktiv",
        )
        if gap is not None and stable_for_learning(control, now) and (
            room_id not in self.last_learning or now - self.last_learning[room_id] >= timedelta(hours=6)
        ):
            old_baseline = baseline
            learn_hold_valve(model, gap, valve)
            baseline = hold_valve(model, gap)
            control.integral -= baseline - old_baseline
            self.learning_counts[room_id] = self.learning_counts.get(room_id, 0) + 1
            self.learning_dirty = True
            self.last_learning[room_id] = now
            result.update(hold_valve=baseline, learning_count=self.learning_counts[room_id])
        self._observe_heating(room_id, now, actual, target, valve, baseline)
        result.update(heating_rate=self.heating_rates.get(room_id, DEFAULT_HEATING_RATE), heating_rate_count=self.heating_rate_counts.get(room_id, 0))
        projected = deepcopy(control)
        for minutes in range(5, 31, 5):
            future = now + timedelta(minutes=minutes)
            future_schedule = round(clamp(schedule_target(room["schedule"], future) + offset, 5, 35), 1)
            future_preheat = preheat_plan(room["schedule"], future, actual, offset, rate, self.preheat_locks.get(room_id))
            future_target = future_preheat["target"] if future_preheat["active"] else future_schedule
            update_control(projected, actual, future_target, future, hold_valve(model, heat_gap(future_target, mean)), heat_gap(future_target, mean), room["minimum_valve"])
        result["valve_30m"] = int(projected.valve + 0.5)
        if current is not None and abs(current - valve_output) < 0.5:
            return
        if not await self._write_valve(room, now, valve_output):
            result.update(state="error", message="Ventilstellung konnte nicht geschrieben werden")
