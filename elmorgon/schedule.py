"""Turn a price forecast into a plan, then cost the plan at the prices that cleared.

Plans are made at the forecast's resolution (hourly) and executed against the
real market intervals (hourly until Sep 2025, 15-minute after): an hour's
energy is spread evenly over its intervals. The perfect-foresight plan works on
the real intervals directly, so it is a true upper bound.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.optimize import linprog


@dataclass(frozen=True)
class EV:
    energy_kwh: float = 10.0  # ~50 km of driving a day
    power_kw: float = 11.0  # three-phase home charger
    home_hours: tuple[int, ...] = (*range(0, 7), *range(18, 24))  # local clock hours
    arrival_hour: int = 18  # when "plug in and charge" starts


@dataclass(frozen=True)
class Battery:
    capacity_kwh: float = 10.0
    power_kw: float = 5.0
    round_trip: float = 0.90
    start_soc_kwh: float = 5.0  # starts and ends every day half full
    wear_sek_per_kwh: float = 0.03  # discharged energy; stops cycling on tiny spreads


def ev_plan(prices: np.ndarray, available: np.ndarray, step_h: float, ev: EV = EV()) -> np.ndarray:
    """Energy (kWh) to charge in each step, minimising cost.

    Charging is a fractional knapsack: no step affects another, so filling the
    cheapest available steps first is optimal. `ev_plan_lp` solves the same
    problem as a linear program, and the tests check that the two agree.
    """
    cap = np.where(available, ev.power_kw * step_h, 0.0)
    if cap.sum() < ev.energy_kwh - 1e-9:
        raise ValueError("not enough charging time for the required energy")
    plan = np.zeros_like(prices, dtype=float)
    need = ev.energy_kwh
    for i in np.argsort(prices, kind="stable"):
        if need <= 0:
            break
        plan[i] = min(cap[i], need)
        need -= plan[i]
    return plan


def ev_plan_lp(
    prices: np.ndarray, available: np.ndarray, step_h: float, ev: EV = EV()
) -> np.ndarray:
    cap = np.where(available, ev.power_kw * step_h, 0.0)
    res = linprog(
        c=prices,
        A_eq=np.ones((1, len(prices))),
        b_eq=[ev.energy_kwh],
        bounds=list(zip(np.zeros_like(cap), cap, strict=True)),
        method="highs",
    )
    if not res.success:
        raise ValueError(res.message)
    return res.x


def ev_on_arrival(local_hours: np.ndarray, step_h: float, ev: EV = EV()) -> np.ndarray:
    """The default: plug in at 18:00 and charge at full power until full."""
    plan = np.zeros(len(local_hours))
    need = ev.energy_kwh
    for i in np.flatnonzero(local_hours >= ev.arrival_hour):
        plan[i] = min(ev.power_kw * step_h, need)
        need -= plan[i]
        if need <= 0:
            break
    return plan


def battery_plan(
    prices: np.ndarray, step_h: float, bat: Battery = Battery()
) -> tuple[np.ndarray, np.ndarray]:
    """Charge and discharge (kWh per step) that maximise arbitrage profit.

    Variables per step: charge c, discharge d, state of charge s.
        minimise   sum(p * (c - d)) + wear * sum(d)
        subject to s[t] = s[t-1] + eta * c[t] - d[t] / eta,  s[-1] = s[end] = start
                   0 <= c, d <= power * step,  0 <= s <= capacity
    """
    n = len(prices)
    eta = np.sqrt(bat.round_trip)
    limit = bat.power_kw * step_h
    cost = np.concatenate([prices, -prices + bat.wear_sek_per_kwh, np.zeros(n)])

    a_eq = np.zeros((n + 1, 3 * n))
    b_eq = np.zeros(n + 1)
    for t in range(n):
        a_eq[t, t] = -eta  # charge
        a_eq[t, n + t] = 1 / eta  # discharge
        a_eq[t, 2 * n + t] = 1  # s[t]
        if t > 0:
            a_eq[t, 2 * n + t - 1] = -1  # s[t-1]
        else:
            b_eq[t] = bat.start_soc_kwh
    a_eq[n, 3 * n - 1] = 1  # end where we started
    b_eq[n] = bat.start_soc_kwh

    bounds = [(0, limit)] * (2 * n) + [(0, bat.capacity_kwh)] * n
    res = linprog(cost, A_eq=a_eq, b_eq=b_eq, bounds=bounds, method="highs")
    if not res.success:
        raise ValueError(res.message)
    return res.x[:n], res.x[n : 2 * n]


def spread_to_intervals(hourly_plan: np.ndarray, intervals_per_hour: np.ndarray) -> np.ndarray:
    """Execute an hourly plan on market intervals: each hour's energy split evenly."""
    return np.repeat(hourly_plan / intervals_per_hour, intervals_per_hour)
