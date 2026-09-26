"""Pure schedule and slow floor-heating control calculations."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from math import exp, isfinite

HEAT_GAP_POINTS = (0.0, 15.0, 30.0, 45.0)
DEFAULT_HOLD_VALVES = (8.0, 28.0, 50.0, 72.0)
DEFAULT_HEATING_RATE = 0.25  # Degrees Celsius per hour during active heating.


def clamp(value: float, minimum: float, maximum: float) -> float:
    """Keep a number inside an inclusive range."""
    return max(minimum, min(maximum, value))


def schedule_target(schedule: list[list[dict]], now: datetime) -> float:
    """Return the last active minute slot, including a preceding day's last slot."""
    minute = now.hour * 60 + now.minute
    for distance in range(7):
        day = (now.weekday() - distance) % 7
        limit = minute if distance == 0 else 1439
        slots = [item for item in schedule[day] if item["minute"] <= limit]
        if slots:
            return float(max(slots, key=lambda item: item["minute"])["temperature"])
    return 20.0


def outdoor_offset(
    daily_mean: float | None,
    cold_offset: float = 1.0,
    warm_offset: float = -1.0,
) -> float:
    """Interpolate each room's correction between -15 and +15 °C outside."""
    if daily_mean is None or not isfinite(daily_mean):
        return 0.0
    fraction = (clamp(daily_mean, -15, 15) + 15) / 30
    return round(cold_offset + (warm_offset - cold_offset) * fraction, 2)


def next_switches(schedule: list[list[dict]], now: datetime, limit: int = 3) -> list[dict]:
    """List the next weekly schedule changes with local timestamps."""
    upcoming: list[tuple[datetime, float]] = []
    for distance in range(8):
        date = (now + timedelta(days=distance)).date()
        day = date.weekday()
        for slot in schedule[day]:
            when = datetime.combine(date, datetime.min.time(), tzinfo=now.tzinfo) + timedelta(minutes=slot["minute"])
            if when > now:
                upcoming.append((when, float(slot["temperature"])))
    return [
        {"time": when.isoformat(), "temperature": temperature}
        for when, temperature in sorted(upcoming, key=lambda item: item[0])[:limit]
    ]


def preheat_plan(
    schedule: list[list[dict]], now: datetime, measured: float | None,
    offset: float, heating_rate: float, locked: dict | None = None,
) -> dict:
    """Start heating ahead of the next upward schedule change."""
    upcoming = next_switches(schedule, now, limit=1)
    if not upcoming or measured is None:
        return {"active": False, "lead_hours": None, "time": None, "target": None}
    next_slot = upcoming[0]
    current_base = schedule_target(schedule, now)
    future_base = next_slot["temperature"]
    when = datetime.fromisoformat(next_slot["time"])
    future_target = round(clamp(future_base + offset, 5, 35), 1)
    if locked and locked.get("time") == next_slot["time"] and future_base > current_base + 0.1:
        return {"active": True, "lead_hours": locked["lead_hours"], "time": next_slot["time"], "target": future_target}
    if future_base <= current_base + 0.1 or future_target <= measured + 0.1:
        return {"active": False, "lead_hours": None, "time": None, "target": None}
    lead_hours = round(clamp((future_target - measured) / max(heating_rate, 0.05) + 0.75, 0, 8), 2)
    remaining = (when - now).total_seconds() / 3600
    return {
        "active": remaining <= lead_hours,
        "lead_hours": lead_hours,
        "time": next_slot["time"],
        "target": future_target,
    }


def observed_heating_rate(samples: list[tuple[datetime, float, float, float]], now: datetime) -> float | None:
    """Estimate a room's rise in °C/h from a sustained heating period."""
    if len(samples) < 36 or now - samples[0][0] < timedelta(minutes=175):
        return None
    if samples[-1][1] - samples[0][1] < 0.15:
        return None
    if any(later[1] < earlier[1] - 0.15 for earlier, later in zip(samples, samples[1:])):
        return None
    if any(valve < baseline + 8 for _, _, valve, baseline in samples):
        return None
    hours = [(stamp - samples[0][0]).total_seconds() / 3600 for stamp, *_ in samples]
    values = [temperature for _, temperature, _, _ in samples]
    mean_hours = sum(hours) / len(hours)
    mean_value = sum(values) / len(values)
    denominator = sum((hour - mean_hours) ** 2 for hour in hours)
    if denominator == 0:
        return None
    slope = sum((hour - mean_hours) * (value - mean_value) for hour, value in zip(hours, values)) / denominator
    return round(clamp(slope, 0.05, 1.5), 3) if slope > 0 else None


def learn_heating_rate(previous: float, observed: float) -> float:
    """Change the saved estimate by at most 0.04 °C/h per six hours."""
    return round(clamp(previous + 0.08 * clamp(observed - previous, -0.5, 0.5), 0.05, 1.5), 3)


def rolling_mean(samples: list[tuple[datetime, float]], now: datetime, hours: float = 24) -> tuple[float | None, float]:
    """Average available outdoor readings over the requested number of hours."""
    recent = [(stamp, value) for stamp, value in samples if now - timedelta(hours=hours) <= stamp <= now and isfinite(value)]
    if not recent:
        return None, 0.0
    coverage = min(hours, max(0.0, (recent[-1][0] - recent[0][0]).total_seconds() / 3600))
    return round(sum(value for _, value in recent) / len(recent), 2), round(coverage, 1)


def heat_gap(target: float, outdoor_mean: float | None) -> float | None:
    """Approximate the heat loss from the inside/outside temperature gap."""
    return max(0.0, target - outdoor_mean) if outdoor_mean is not None else None


def hold_valve(model: list[float], gap: float | None) -> float:
    """Interpolate a room's learned holding valve for the heat demand."""
    if gap is None:
        return 30.0
    gap = clamp(gap, HEAT_GAP_POINTS[0], HEAT_GAP_POINTS[-1])
    for index in range(len(HEAT_GAP_POINTS) - 1):
        left, right = HEAT_GAP_POINTS[index:index + 2]
        if gap <= right:
            fraction = (gap - left) / (right - left)
            return round(model[index] * (1 - fraction) + model[index + 1] * fraction, 2)
    return round(model[-1], 2)


def learn_hold_valve(model: list[float], gap: float, observed: float) -> list[float]:
    """Nudge the nearby learned values toward a stable observed valve."""
    predicted = hold_valve(model, gap)
    error = clamp(observed - predicted, -20, 20)
    gap = clamp(gap, HEAT_GAP_POINTS[0], HEAT_GAP_POINTS[-1])
    for index, point in enumerate(HEAT_GAP_POINTS):
        weight = max(0.0, 1 - abs(gap - point) / 15)
        # One update changes a model point by at most 0.4 percentage points.
        # With the six-hour interval this takes days, not hours, to adapt.
        model[index] = round(clamp(model[index] + 0.02 * error * weight, 5, 90), 3)
    return model


@dataclass
class ControlState:
    """Transient state; restored conservatively after a restart."""

    filtered: float | None = None
    previous_raw: float | None = None
    integral: float = 0.0  # Slowly adapting correction around the holding valve.
    valve: float = 0.0
    last_update: datetime | None = None
    last_switch_at: datetime | None = None
    last_target: float | None = None
    window_until: datetime | None = None
    window_reference: float | None = None
    window_checked_at: datetime | None = None
    recent: list[tuple[datetime, float]] | None = None
    manual_until: datetime | None = None
    manual_value: float | None = None
    stable_samples: list[tuple[datetime, float, float, float, float | None]] | None = None
    cooling_samples: list[tuple[datetime, float, float]] | None = None


def approaching_target_while_cooling(samples: list[tuple[datetime, float, float]], measured: float, target: float) -> bool:
    """Recognize a sustained, gentle fall likely to reach the target soon."""
    if not 0 <= measured - target <= 0.45 or len(samples) < 6:
        return False
    hours = (samples[-1][0] - samples[0][0]).total_seconds() / 3600
    if hours < 0.5 or max(item[2] for item in samples) - min(item[2] for item in samples) > 0.05:
        return False
    temperatures = [item[1] for item in samples]
    if any(earlier - later > 0.25 for earlier, later in zip(temperatures, temperatures[1:])):
        return False
    declines = [index for index in range(len(temperatures) - 3)
                if temperatures[index] - temperatures[index + 3] >= 0.05]
    if len(declines) < 2 or declines[-1] - declines[0] < 4:
        return False
    drop = sum(temperatures[:3]) / 3 - sum(temperatures[-3:]) / 3
    cooling_rate = (temperatures[-1] - temperatures[0]) / hours
    return drop >= 0.1 and cooling_rate <= -0.12 and measured + cooling_rate <= target + 0.05


def stable_for_learning(state: ControlState, now: datetime) -> bool:
    """Only learn from three hours of steady temperature, target and valve."""
    samples = state.stable_samples or []
    if len(samples) < 24 or now - samples[0][0] < timedelta(minutes=175):
        return False
    temperatures = [sample[1] for sample in samples]
    valves = [sample[2] for sample in samples]
    targets = [sample[3] for sample in samples]
    gaps = [sample[4] for sample in samples]
    return (
        max(temperatures) - min(temperatures) <= 0.2
        and max(valves) - min(valves) <= 4
        and max(targets) - min(targets) <= 0.1
        and all(gap is not None for gap in gaps)
        and max(gaps) - min(gaps) <= 2
        and abs(temperatures[-1] - targets[-1]) <= 0.2
        and 5 <= valves[-1] <= 90
    )


def detect_open_window(state: ControlState, measured: float, outdoor: float | None, now: datetime) -> tuple[bool, float | None]:
    """Estimate an open window from a fast room-temperature drop in cold weather.

    This updates display-only observations. The valve controller does not read them.
    """
    recent = [(stamp, value) for stamp, value in (state.recent or []) if now - timedelta(minutes=30) <= stamp <= now]
    elapsed = (now - state.window_checked_at).total_seconds() if state.window_checked_at else None
    reference = state.previous_raw
    drop = max((value - measured for _, value in recent), default=0.0)
    sudden = reference is not None and reference - measured >= 0.5
    if outdoor is not None and measured - outdoor >= 3 and elapsed is not None and 0 < elapsed <= 900 and (sudden or drop >= 0.8):
        state.window_until = now + timedelta(minutes=90)
        state.window_reference = max([reference if reference is not None else measured, *(value for _, value in recent)])
    elif state.window_until and (now >= state.window_until or outdoor is None or measured - outdoor < 3 or
                                  (state.window_reference is not None and measured >= state.window_reference - 0.2)):
        state.window_until = None
        state.window_reference = None
    recent.append((now, measured))
    state.recent = recent
    state.previous_raw = measured
    state.window_checked_at = now
    return state.window_until is not None and now < state.window_until, round(drop, 1) if drop > 0 else None


def update_control(
    state: ControlState,
    measured: float,
    target: float,
    now: datetime,
    baseline: float = 30.0,
    gap: float | None = None,
    minimum_valve: float = 0.0,
) -> tuple[ControlState, float]:
    """Low-pass PI controller with an output slew limit.

    Each tick is expected about every five minutes. Long gaps are capped so a
    restart or offline sensor cannot accumulate hours of fictitious error.
    """
    if not isfinite(measured) or not -20 <= measured <= 60:
        raise ValueError("Invalid room temperature")
    if not isfinite(target) or not 5 <= target <= 35:
        raise ValueError("Invalid target temperature")
    if not isfinite(minimum_valve) or not 0 <= minimum_valve <= 100:
        raise ValueError("Invalid minimum valve position")

    samples = [sample for sample in (state.cooling_samples or []) if now - sample[0] <= timedelta(minutes=60)]
    if samples and abs(samples[-1][2] - target) > 0.05:
        samples = []
    if not samples or now - samples[-1][0] >= timedelta(minutes=4):
        samples.append((now, measured, target))
    state.cooling_samples = samples

    elapsed = 0.0
    if state.last_update is not None:
        elapsed = clamp((now - state.last_update).total_seconds(), 0.0, 900.0)
    filtered = measured if state.filtered is None else state.filtered
    if elapsed:
        filtered += (measured - filtered) * (1 - exp(-elapsed / 5400.0))

    if elapsed:
        was_open = state.valve > 0
        target_change = target - state.last_target if state.last_target is not None else 0
        error = target - filtered
        state.integral = clamp(state.integral + error * elapsed / 3600 * 1.5, -90, 90)
        proposed = clamp(baseline + state.integral + 12 * error, 0, 100)
        if (gap is None or gap > 3) and abs(error) <= 0.15:
            proposed = max(proposed, 5)
        # Open before an approaching target is crossed when the room has been
        # cooling steadily. Ignore brief sensor jumps and recent target changes.
        if approaching_target_while_cooling(samples, measured, target):
            proposed = max(proposed, minimum_valve / 2 + 2.5 if minimum_valve else 5)
        # Once opened, the valve stays at or above its useful minimum. A small
        # hysteresis around half that minimum avoids repeated on/off switching.
        if minimum_valve:
            open_at = minimum_valve / 2 + 2.5
            close_at = max(0, minimum_valve / 2 - 2.5)
            if state.valve <= 0 and proposed < open_at:
                proposed = 0
            elif state.valve > 0 and proposed > close_at:
                proposed = max(proposed, minimum_valve)
            else:
                proposed = 0 if state.valve > 0 else max(proposed, minimum_valve)
            # Floor heating responds slowly: hold each on/off state long enough
            # to avoid cycling around the threshold. Large temperature or plan
            # changes can still override the hold immediately.
            if state.last_switch_at is not None:
                since_switch = now - state.last_switch_at
                if was_open and proposed == 0 and since_switch < timedelta(minutes=30) \
                        and measured < target + 0.5 and target_change > -0.2:
                    proposed = minimum_valve
                elif not was_open and proposed > 0 and since_switch < timedelta(minutes=15) \
                        and measured > target - 0.3 and target_change < 0.2:
                    proposed = 0
        # Continuous movement is limited to 5 percentage points per 15 minutes.
        # Crossing the useless 0..minimum band switches directly to 0 or minimum.
        step = 5 * elapsed / 900
        if minimum_valve and state.valve <= 0 < proposed:
            state.valve = minimum_valve
        elif minimum_valve and proposed <= 0 < state.valve:
            state.valve = max(minimum_valve, state.valve - step) if state.valve > minimum_valve else 0
        elif minimum_valve and 0 < state.valve < minimum_valve:
            state.valve = minimum_valve if proposed > 0 else 0
        else:
            state.valve = clamp(proposed, state.valve - step, state.valve + step)
        if minimum_valve and (state.valve > 0) != was_open:
            state.last_switch_at = now
    state.filtered = filtered
    state.last_update = now
    state.last_target = target
    samples = [(stamp, temp, valve, wanted, previous_gap) for stamp, temp, valve, wanted, previous_gap in (state.stable_samples or []) if now - stamp <= timedelta(hours=3)]
    samples.append((now, measured, state.valve, target, gap))
    state.stable_samples = samples
    return state, round(clamp(state.valve, 0, 100), 1)
