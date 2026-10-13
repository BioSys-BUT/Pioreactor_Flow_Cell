# -*- coding: utf-8 -*-
import threading
import time
from configparser import NoOptionError

import click

from pioreactor.actions.pump import PWMPump
from pioreactor.actions.pump import get_default_calibration
from pioreactor.actions.pump import is_default_calibration
from pioreactor.background_jobs.base import BackgroundJobContrib
from pioreactor.calibrations import load_active_calibration
from pioreactor.config import config
from pioreactor.exc import PWMError
from pioreactor.hardware import get_pwm_to_pin_map
from pioreactor.utils import clamp
from pioreactor.whoami import get_assigned_experiment_name
from pioreactor.whoami import get_unit_name


__plugin_summary__ = "Run two pumps together to circulate liquid through a flow cell."
__plugin_version__ = "0.1.0"
__plugin_name__ = "flowcell"


def __dir__():
    return ["click_flowcell"]


_ALLOWED_PUMPS = ("media", "waste", "alt_media")
_CONFIG_SECTION = "flowcell.config"


def _parse_pumps(raw: str) -> list[str]:
    names = []
    for part in raw.split(","):
        name = part.strip().lower().replace("-", "_").replace(" ", "_")
        if name.endswith("_pump"):
            name = name[: -len("_pump")]
        if name:
            names.append(name)

    if len(names) != 2 or len(set(names)) != 2 or any(name not in _ALLOWED_PUMPS for name in names):
        raise ValueError(
            "pumps must be two different names from media, waste, alt_media. "
            f"Got {raw!r}."
        )
    return names


def _flow_ml_per_second(calibration) -> float | None:
    duration_to_ml = getattr(calibration, "duration_to_ml", None)
    if callable(duration_to_ml):
        return float(duration_to_ml(1.0))
    x_to_y = getattr(calibration, "x_to_y", None)
    if callable(x_to_y):
        return float(x_to_y(1))
    return None


class Flowcell(BackgroundJobContrib):
    """
    Run two peristaltic pumps continuously for a closed vial <-> flow cell loop.

    speed_percent is the PWM duty cycle sent to both pumps. On start, with no
    speed_percent in config, it is the calibrated duty cycle.
    """

    job_name = "flowcell"
    published_settings = {
        "speed_percent": {"datatype": "float", "settable": True, "unit": "%"},
    }

    def __init__(self, unit, experiment, **kwargs) -> None:
        super().__init__(unit=unit, experiment=experiment, plugin_name="flowcell", **kwargs)
        self._pumps: list[PWMPump] = []
        self._pump_names: list[str] = []
        self._outputs_started = False
        self._start_failed = False
        self._stop_after_transition = False
        self._runtime_lock = threading.Lock()
        self._runtime_timer: threading.Timer | None = None
        self._runtime_remaining_s: float | None = None
        self._runtime_started_monotonic: float | None = None

        try:
            if not config.has_section(_CONFIG_SECTION):
                self.logger.warning(
                    "Missing [%s]. Using pumps=media,waste and max_runtime_min=0. "
                    "Flow speed will be the calibrated duty cycle. "
                    "Append that section to config.ini so Advanced settings can edit pumps and runtime.",
                    _CONFIG_SECTION,
                )
            self._pump_names = _parse_pumps(config.get(_CONFIG_SECTION, "pumps", fallback="media,waste"))
            requested_speed = self._requested_speed_from_config()
            max_runtime_min = config.getfloat(_CONFIG_SECTION, "max_runtime_min", fallback=0.0)
            if max_runtime_min < 0:
                raise ValueError("max_runtime_min must be 0 or greater.")
            if max_runtime_min > 0:
                self._runtime_remaining_s = max_runtime_min * 60.0
            self._open_pumps()
            self._warn_if_calibrations_differ()
            self._calibrated_duty = self._calibrated_duty_cycle()
            if requested_speed is None:
                self.speed_percent = self._calibrated_duty
            else:
                self.speed_percent = requested_speed
                self.logger.info(
                    "Flow speed %g%% from config. Active calibration duty cycle is %g%%.",
                    self.speed_percent,
                    self._calibrated_duty,
                )
        except PWMError:
            self._release_pumps()
            raise
        except Exception as exc:
            self._release_pumps()
            self.logger.error("%s", exc)
            raise

    def _requested_speed_from_config(self) -> float | None:
        """
        A number in config is an absolute PWM percent for both pumps.
        A missing or blank value means "use the calibrated duty cycle".
        """
        if not config.has_option(_CONFIG_SECTION, "speed_percent"):
            return None
        raw = config.get(_CONFIG_SECTION, "speed_percent").strip()
        if raw == "" or raw.lower() == "auto":
            return None
        try:
            speed = float(raw)
        except ValueError as exc:
            raise ValueError(
                "speed_percent must be a PWM duty cycle from 0 to 100, or auto to use the calibration. "
                f"Got {raw!r}."
            ) from exc
        if not 0.0 <= speed <= 100.0:
            raise ValueError("speed_percent must be from 0 to 100. Got %s." % raw)
        return speed

    def _calibrated_duty_cycle(self) -> float:
        duties = [float(pump.calibration.dc) for pump in self._pumps]
        if abs(duties[0] - duties[1]) <= 0.05:
            return round(duties[0], 2)
        # One number drives both pumps. Start at the lower calibrated duty cycle.
        return round(min(duties), 2)

    def _calibration_for(self, pump_name: str):
        device = f"{pump_name}_pump"
        calibration = load_active_calibration(device)
        if calibration is None or is_default_calibration(calibration):
            self.logger.warning(
                "No active calibration for %s. Using the default 100%% duty cycle at 250 Hz. "
                "Copy the same calibration onto both pumps so their flow rates match.",
                device,
            )
            return get_default_calibration()
        return calibration

    def _pin_for(self, pump_name: str):
        try:
            channel = config.get("PWM_reverse", pump_name)
        except NoOptionError:
            self.logger.error(
                "Config entry not found. Add `%s` to the PWM section in config.ini.",
                pump_name,
            )
            raise
        try:
            return get_pwm_to_pin_map()[channel]
        except KeyError:
            self.logger.error("PWM channel %s for %s is not a known pin.", channel, pump_name)
            raise

    def _open_pumps(self) -> None:
        for pump_name in self._pump_names:
            try:
                pump = PWMPump(
                    self.unit,
                    self.experiment,
                    self._pin_for(pump_name),
                    calibration=self._calibration_for(pump_name),
                    mqtt_client=self.pub_client,
                    logger=self.logger,
                )
            except PWMError:
                self.logger.error(
                    "Could not lock %s. Another job is using that pump. Flow cell will not leave the other pump running.",
                    pump_name,
                )
                raise
            self._pumps.append(pump)

    def _warn_if_calibrations_differ(self) -> None:
        left, right = self._pumps
        left_cal, right_cal = left.calibration, right.calibration
        reasons = []
        if abs(float(left_cal.dc) - float(right_cal.dc)) > 0.05:
            reasons.append(f"duty cycle {left_cal.dc} vs {right_cal.dc}")
        if abs(float(left_cal.hz) - float(right_cal.hz)) > 0.05:
            reasons.append(f"frequency {left_cal.hz} vs {right_cal.hz} Hz")

        left_flow = _flow_ml_per_second(left_cal)
        right_flow = _flow_ml_per_second(right_cal)
        if left_flow is not None and right_flow is not None and abs(left_flow - right_flow) > 0.01:
            reasons.append(f"flow {left_flow:.4f} vs {right_flow:.4f} mL/s")

        if reasons:
            self.logger.warning(
                "Pump calibrations differ (%s). Matched calibrations keep the flow-cell loop from pulling a vacuum or making bubbles.",
                "; ".join(reasons),
            )

    def _applied_duty(self) -> float:
        # The same PWM percent goes to both pumps. It is not scaled by calibration.dc.
        return clamp(0.0, float(self.speed_percent), 100.0)

    def _duty_description(self) -> str:
        duty = self._applied_duty()
        return ", ".join(f"{name} {duty:.2f}%" for name in self._pump_names)

    def _apply_running_duty(self) -> None:
        # PWMPump.continuously() always uses the full calibrated duty cycle, so speed is applied here.
        duty_cycle = self._applied_duty()
        for pump in self._pumps:
            if self._outputs_started:
                pump.interrupt.clear()
                pump.pwm.change_duty_cycle(duty_cycle)
            else:
                pump.start(duty_cycle)
        self._outputs_started = True

    def _halt_pumps(self) -> None:
        for pump in self._pumps:
            try:
                pump.stop()
            except Exception as exc:
                self.logger.debug("Error stopping a flow-cell pump: %s", exc)

    def _release_pumps(self) -> None:
        self._halt_pumps()
        while self._pumps:
            pump = self._pumps.pop()
            try:
                pump.clean_up()
            except Exception as exc:
                self.logger.debug("Error releasing a flow-cell pump: %s", exc)
        self._outputs_started = False

    def _start_runtime_clock(self) -> bool:
        """
        Start the pumping-time countdown.

        Returns False when the limit has already been used up. Callers must stop the job
        after the state transition finishes; stopping from inside a transition hook is undone
        when the base class then enters ready.
        """
        with self._runtime_lock:
            if self._runtime_remaining_s is None:
                return True
            if self._runtime_remaining_s <= 0:
                return False
            self._runtime_started_monotonic = time.monotonic()
            timer = threading.Timer(self._runtime_remaining_s, self._on_runtime_elapsed)
            timer.daemon = True
            self._runtime_timer = timer
            timer.start()
            self.logger.info(
                "Flow cell will stop after %.2f more minutes of pumping.",
                self._runtime_remaining_s / 60.0,
            )
            return True

    def _pause_runtime_clock(self) -> None:
        if not hasattr(self, "_runtime_lock"):
            return
        with self._runtime_lock:
            timer = self._runtime_timer
            self._runtime_timer = None
            started = self._runtime_started_monotonic
            self._runtime_started_monotonic = None
            if timer is not None:
                timer.cancel()
            if started is not None and self._runtime_remaining_s is not None:
                elapsed = time.monotonic() - started
                self._runtime_remaining_s = max(0.0, self._runtime_remaining_s - elapsed)

    def _on_runtime_elapsed(self) -> None:
        with self._runtime_lock:
            self._runtime_timer = None
            self._runtime_remaining_s = 0.0
            self._runtime_started_monotonic = None
        if getattr(self, "_shutdown_started", False) or self.state == self.DISCONNECTED:
            return
        self.logger.info("Flow cell reached its max runtime. Stopping.")
        self.clean_up()

    def set_speed_percent(self, speed_percent: float) -> None:
        self.speed_percent = clamp(0.0, float(speed_percent), 100.0)
        if self.state != self.READY or not self._outputs_started:
            return
        try:
            self._apply_running_duty()
        except Exception as exc:
            self.logger.error("Failed to update flow speed: %s", exc)
            self._halt_pumps()
            self.clean_up()
            return
        self.logger.info("Flow speed is now %.1f%% (%s).", self.speed_percent, self._duty_description())

    def _begin_pumping(self) -> None:
        try:
            self._apply_running_duty()
        except Exception as exc:
            self.logger.error("Failed to start both flow-cell pumps. Stopping both. %s", exc)
            self._halt_pumps()
            self._start_failed = True
            return
        self.logger.info(
            "Running %s continuously at %g%% PWM (%s). Calibrated duty cycle is %g%%.",
            " and ".join(self._pump_names),
            self.speed_percent,
            self._duty_description(),
            self._calibrated_duty,
        )
        if not self._start_runtime_clock():
            self._halt_pumps()
            self._stop_after_transition = True

    def on_ready(self) -> None:
        # Reached after on_init_to_ready and on_sleeping_to_ready. Stop here, not inside those hooks.
        if self._start_failed or self._stop_after_transition:
            if self._stop_after_transition and not self._start_failed:
                self.logger.info("Flow cell reached its max runtime. Stopping.")
            self.clean_up()

    def on_init_to_ready(self) -> None:
        self._begin_pumping()

    def on_ready_to_sleeping(self) -> None:
        self._pause_runtime_clock()
        self._halt_pumps()
        self.logger.info("Paused flow cell pumps.")

    def on_sleeping_to_ready(self) -> None:
        self._begin_pumping()

    def on_disconnected(self) -> None:
        self._pause_runtime_clock()
        self._release_pumps()


@click.command(name="flowcell", help=__plugin_summary__)
def click_flowcell():
    """
    Run two pumps continuously for a flow cell.
    """
    unit = get_unit_name()
    job = Flowcell(
        unit=unit,
        experiment=get_assigned_experiment_name(unit),
    )
    job.block_until_disconnected()
