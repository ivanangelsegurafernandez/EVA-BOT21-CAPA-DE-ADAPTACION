#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
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
    """Motor operativo con anti-plano suave y compuertas explícitas.

    Objetivo: evitar quedarse "pegado" en probabilidades bajas cuando hay
    señales de momentum (racha), pero sin romper reglas de seguridad.
    """

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
        # Escalado suave: si densidad está por encima de 50%, permite impulso gradual.
        rel = (density - 0.50) / 0.50
        return self._clamp(rel * self.cfg.soft_boost_max, 0.0, self.cfg.soft_boost_max)

    def _dynamic_cap(self, t: TickInput) -> float:
        if t.reliable and t.feature_count >= self.cfg.min_features_for_reliable:
            return 0.85
        # Si no es confiable, cap más estricto para contener sobre-exposición.
        return self.cfg.cap_unreliable

    def evaluate(self, t: TickInput) -> dict[str, Any]:
        p_raw = self._clamp(t.p_raw, 0.0, 1.0)
        boost = self._streak_boost(t.streak_wins, t.streak_window)

        # Anti-plano suave: solo activa cuando hay algo de base, evita inflar ruido.
        anti_flat = 0.0
        if p_raw >= (self.cfg.unrel_gate - self.cfg.boost_activation_gap):
            anti_flat = boost

        p_pre = self._clamp(p_raw + anti_flat, 0.0, 0.95)
        cap = self._dynamic_cap(t)
        p_cap = self._clamp(p_pre, 0.0, cap)

        why_no: list[str] = []
        p_oper = p_cap

        # Compuertas operativas principales.
        if not t.reliable:
            if p_cap < self.cfg.unrel_gate:
                why_no.append(f"p_best<{self.cfg.unrel_gate*100:.1f}%")
                p_oper = 0.0
        else:
            if p_cap < self.cfg.real_gate:
                why_no.append(f"p_best<{self.cfg.real_gate*100:.1f}%")
                p_oper = 0.0

        if not t.confirm_ok:
            why_no.append("confirm_pending")
            p_oper = 0.0
        if not t.trigger_ok:
            why_no.append("trigger_no")
            p_oper = 0.0

        # Estado de madurez: evita conclusiones fuertes sin cierres.
        maturity = "mature" if t.closed_signals >= 50 else "low_sample"

        return {
            "p_raw": round(p_raw, 4),
            "boost": round(boost, 4),
            "anti_flat": round(anti_flat, 4),
            "p_pre": round(p_pre, 4),
            "cap": round(cap, 4),
            "p_cap": round(p_cap, 4),
            "p_oper": round(p_oper, 4),
            "maturity": maturity,
            "why_no": why_no or ["none"],
        }


def _sample_ticks() -> list[TickInput]:
    return [
        TickInput(0.21, 5, 8, False, 3, 0, False, False),
        TickInput(0.43, 6, 8, False, 3, 10, True, False),
        TickInput(0.49, 7, 8, False, 3, 20, True, True),
        TickInput(0.58, 6, 8, True, 6, 80, True, True),
    ]


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
    args = ap.parse_args()

    engine = ProbabilityEngine()

    if args.self_test:
        out = [engine.evaluate(t) for t in _sample_ticks()]
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    if args.p_raw is None:
        ap.error("Debes pasar --p-raw o usar --self-test")

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
