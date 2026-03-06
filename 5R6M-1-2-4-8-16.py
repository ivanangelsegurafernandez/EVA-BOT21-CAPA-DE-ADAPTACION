#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from typing import Any


@dataclass
class EngineConfig:
    unrel_gate: float = 0.50
    real_gate: float = 0.60
    cap_unreliable: float = 0.66
    soft_boost_max: float = 0.08
    boost_activation_gap: float = 0.10
    min_features_for_reliable: int = 5


@dataclass
class TickInput:
    p_raw: float
    streak_wins: int
    streak_window: int
    reliable: bool
    feature_count: int
    closed_signals: int
    confirm_ok: bool
    trigger_ok: bool


class ProbabilityEngine:
    """Motor operativo con anti-plano suave y compuertas explícitas."""

    def __init__(self, cfg: EngineConfig | None = None) -> None:
        self.cfg = cfg or EngineConfig()

    @staticmethod
    def _clamp(v: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, v))

    def _streak_boost(self, wins: int, window: int) -> float:
        if window <= 0:
            return 0.0
        density = self._clamp(wins / window, 0.0, 1.0)
        if density <= 0.50:
            return 0.0
        rel = (density - 0.50) / 0.50
        return self._clamp(rel * self.cfg.soft_boost_max, 0.0, self.cfg.soft_boost_max)

    def _dynamic_cap(self, t: TickInput) -> float:
        if t.reliable and t.feature_count >= self.cfg.min_features_for_reliable:
            return 0.85
        return self.cfg.cap_unreliable

    def evaluate(self, t: TickInput) -> dict[str, Any]:
        p_raw = self._clamp(t.p_raw, 0.0, 1.0)
        boost = self._streak_boost(t.streak_wins, t.streak_window)

        # Anti-plano: activa sólo cerca de compuerta para no inflar ruido bajo.
        anti_flat = 0.0
        if p_raw >= (self.cfg.unrel_gate - self.cfg.boost_activation_gap):
            anti_flat = boost

        p_pre = self._clamp(p_raw + anti_flat, 0.0, 0.95)
        cap = self._dynamic_cap(t)
        p_cap = self._clamp(p_pre, 0.0, cap)

        why_no: list[str] = []
        p_oper = p_cap

        gate = self.cfg.real_gate if t.reliable else self.cfg.unrel_gate
        if p_cap < gate:
            why_no.append(f"p_best<{gate*100:.1f}%")
            p_oper = 0.0

        if not t.confirm_ok:
            why_no.append("confirm_pending")
            p_oper = 0.0
        if not t.trigger_ok:
            why_no.append("trigger_no")
            p_oper = 0.0

        maturity = "mature" if t.closed_signals >= 50 else "low_sample"
        gap_to_gate = self._clamp(gate - p_cap, 0.0, 1.0)

        return {
            "p_raw": round(p_raw, 4),
            "boost": round(boost, 4),
            "anti_flat": round(anti_flat, 4),
            "p_pre": round(p_pre, 4),
            "cap": round(cap, 4),
            "p_cap": round(p_cap, 4),
            "p_oper": round(p_oper, 4),
            "gate": round(gate, 4),
            "gap_to_gate": round(gap_to_gate, 4),
            "maturity": maturity,
            "why_no": why_no or ["none"],
            "decision": "GO" if p_oper > 0 else "NO_GO",
        }


def parse_why_no_line(line: str) -> dict[str, Any]:
    """Extrae señales básicas de una línea tipo HUD WHY-NO.

    Ejemplo esperado:
    WHY-NO: ... reliable=no ... p_raw=13.0% p_pre=21.0% ... why=p_best<50.0%,confirm_pending(0/1),trigger_no
    """
    txt = line or ""
    m_raw = re.search(r"p_raw=([0-9]+(?:\.[0-9]+)?)%", txt)
    p_raw = float(m_raw.group(1)) / 100.0 if m_raw else 0.0

    m_rel = re.search(r"reliable=(yes|no|true|false)", txt, flags=re.IGNORECASE)
    reliable = False
    if m_rel:
        reliable = m_rel.group(1).lower() in {"yes", "true"}

    m_feat = re.search(r"feats=([0-9]+)", txt)
    feat_count = int(m_feat.group(1)) if m_feat else 3

    why_part = ""
    m_why = re.search(r"why=([^|]+)", txt)
    if m_why:
        why_part = m_why.group(1)
    tokens = {x.strip() for x in why_part.split(",") if x.strip()}

    confirm_ok = not any(tok.startswith("confirm_pending") for tok in tokens)
    trigger_ok = "trigger_no" not in tokens

    return {
        "p_raw": p_raw,
        "reliable": reliable,
        "feature_count": feat_count,
        "confirm_ok": confirm_ok,
        "trigger_ok": trigger_ok,
        "raw_tokens": sorted(tokens),
    }


def _sample_ticks() -> list[TickInput]:
    return [
        TickInput(0.21, 5, 8, False, 3, 0, False, False),
        TickInput(0.43, 6, 8, False, 3, 10, True, False),
        TickInput(0.49, 7, 8, False, 3, 20, True, True),
        TickInput(0.58, 6, 8, True, 6, 80, True, True),
    ]


def _simulate_grid(engine: ProbabilityEngine, reliable: bool, feature_count: int, confirm_ok: bool, trigger_ok: bool) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for step in range(10, 71, 5):
        p = step / 100.0
        t = TickInput(
            p_raw=p,
            streak_wins=6,
            streak_window=8,
            reliable=reliable,
            feature_count=feature_count,
            closed_signals=80,
            confirm_ok=confirm_ok,
            trigger_ok=trigger_ok,
        )
        out = engine.evaluate(t)
        rows.append({"p_raw": p, "p_pre": out["p_pre"], "p_cap": out["p_cap"], "p_oper": out["p_oper"], "decision": out["decision"], "gap_to_gate": out["gap_to_gate"]})
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="5R6M master engine: probabilidad + compuertas")
    ap.add_argument("--p-raw", type=float, default=None)
    ap.add_argument("--wins", type=int, default=0)
    ap.add_argument("--window", type=int, default=8)
    ap.add_argument("--reliable", action="store_true")
    ap.add_argument("--feature-count", type=int, default=3)
    ap.add_argument("--closed", type=int, default=0)
    ap.add_argument("--confirm-ok", action="store_true")
    ap.add_argument("--trigger-ok", action="store_true")
    ap.add_argument("--self-test", action="store_true", help="Ejecuta escenarios ejemplo")
    ap.add_argument("--simulate-grid", action="store_true", help="Simula p_raw de 10% a 70% con configuración actual")
    ap.add_argument("--from-why-no-line", type=str, default="", help="Parsea una línea WHY-NO del HUD y evalúa automáticamente")
    args = ap.parse_args()

    engine = ProbabilityEngine()

    if args.self_test:
        out = [engine.evaluate(t) for t in _sample_ticks()]
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    if args.simulate_grid:
        out = _simulate_grid(engine, args.reliable, args.feature_count, args.confirm_ok, args.trigger_ok)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    if args.from_why_no_line:
        parsed = parse_why_no_line(args.from_why_no_line)
        tick = TickInput(
            p_raw=float(parsed["p_raw"]),
            streak_wins=int(args.wins),
            streak_window=int(args.window),
            reliable=bool(parsed["reliable"]),
            feature_count=int(parsed["feature_count"]),
            closed_signals=int(args.closed),
            confirm_ok=bool(parsed["confirm_ok"]),
            trigger_ok=bool(parsed["trigger_ok"]),
        )
        out = engine.evaluate(tick)
        print(json.dumps({"parsed": parsed, "evaluation": out}, ensure_ascii=False, indent=2))
        return 0

    if args.p_raw is None:
        ap.error("Debes pasar --p-raw, usar --from-why-no-line, --simulate-grid o --self-test")

    tick = TickInput(
        p_raw=float(args.p_raw),
        streak_wins=int(args.wins),
        streak_window=int(args.window),
        reliable=bool(args.reliable),
        feature_count=int(args.feature_count),
        closed_signals=int(args.closed),
        confirm_ok=bool(args.confirm_ok),
        trigger_ok=bool(args.trigger_ok),
    )
    print(json.dumps(engine.evaluate(tick), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
