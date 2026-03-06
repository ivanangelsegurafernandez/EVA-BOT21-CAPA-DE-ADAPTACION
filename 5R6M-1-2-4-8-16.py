#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass
class EngineConfig:
    """Configuración del motor 5R6M."""

    unrel_gate: float = 0.50
    real_gate: float = 0.60
    cap_unreliable: float = 0.66
    cap_reliable: float = 0.85
    soft_boost_max: float = 0.08
    boost_activation_gap: float = 0.10
    min_features_for_reliable: int = 5
    mature_closed_signals: int = 50

    @classmethod
    def from_json_file(cls, path: Path) -> "EngineConfig":
        raw = json.loads(path.read_text(encoding="utf-8"))
        allowed = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        data = {k: v for k, v in raw.items() if k in allowed}
        return cls(**data)


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
    """Motor de probabilidad con anti-plano, cap dinámico y compuertas."""

    def __init__(self, cfg: EngineConfig | None = None) -> None:
        self.cfg = cfg or EngineConfig()

    @staticmethod
    def _clamp(value: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, value))

    def _streak_density(self, wins: int, window: int) -> float:
        if window <= 0:
            return 0.0
        return self._clamp(wins / window, 0.0, 1.0)

    def _streak_boost(self, wins: int, window: int) -> float:
        density = self._streak_density(wins, window)
        if density <= 0.50:
            return 0.0
        rel = (density - 0.50) / 0.50
        return self._clamp(rel * self.cfg.soft_boost_max, 0.0, self.cfg.soft_boost_max)

    def _dynamic_cap(self, reliable: bool, feature_count: int) -> float:
        if reliable and feature_count >= self.cfg.min_features_for_reliable:
            return self.cfg.cap_reliable
        return self.cfg.cap_unreliable

    def evaluate(self, tick: TickInput) -> dict[str, Any]:
        p_raw = self._clamp(tick.p_raw, 0.0, 1.0)
        density = self._streak_density(tick.streak_wins, tick.streak_window)
        boost = self._streak_boost(tick.streak_wins, tick.streak_window)

        anti_flat = 0.0
        if p_raw >= (self.cfg.unrel_gate - self.cfg.boost_activation_gap):
            anti_flat = boost

        p_pre = self._clamp(p_raw + anti_flat, 0.0, 0.95)
        cap = self._dynamic_cap(tick.reliable, tick.feature_count)
        p_cap = self._clamp(p_pre, 0.0, cap)

        gate = self.cfg.real_gate if tick.reliable else self.cfg.unrel_gate
        p_oper = p_cap
        why_no: list[str] = []

        if p_cap < gate:
            why_no.append(f"p_best<{gate*100:.1f}%")
            p_oper = 0.0
        if not tick.confirm_ok:
            why_no.append("confirm_pending")
            p_oper = 0.0
        if not tick.trigger_ok:
            why_no.append("trigger_no")
            p_oper = 0.0

        maturity = "mature" if tick.closed_signals >= self.cfg.mature_closed_signals else "low_sample"
        gap_to_gate = self._clamp(gate - p_cap, 0.0, 1.0)

        return {
            "input": asdict(tick),
            "p_raw": round(p_raw, 4),
            "streak_density": round(density, 4),
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
    text = line or ""

    m_raw = re.search(r"p_raw=([0-9]+(?:\.[0-9]+)?)%", text)
    p_raw = float(m_raw.group(1)) / 100.0 if m_raw else 0.0

    m_rel = re.search(r"reliable=(yes|no|true|false)", text, flags=re.IGNORECASE)
    reliable = bool(m_rel and m_rel.group(1).lower() in {"yes", "true"})

    m_feats = re.search(r"(?:feats|features)=([0-9]+)", text)
    feature_count = int(m_feats.group(1)) if m_feats else 3

    m_closed = re.search(r"closed=([0-9]+)", text)
    closed_signals = int(m_closed.group(1)) if m_closed else 0

    why_text = ""
    m_why = re.search(r"why=([^|]+)", text)
    if m_why:
        why_text = m_why.group(1)
    tokens = sorted({tok.strip() for tok in why_text.split(",") if tok.strip()})

    confirm_ok = not any(tok.startswith("confirm_pending") for tok in tokens)
    trigger_ok = "trigger_no" not in tokens

    return {
        "p_raw": p_raw,
        "reliable": reliable,
        "feature_count": feature_count,
        "closed_signals": closed_signals,
        "confirm_ok": confirm_ok,
        "trigger_ok": trigger_ok,
        "raw_tokens": tokens,
    }


def tick_from_mapping(raw: dict[str, Any]) -> TickInput:
    return TickInput(
        p_raw=float(raw.get("p_raw", 0.0)),
        streak_wins=int(raw.get("streak_wins", raw.get("wins", 0))),
        streak_window=int(raw.get("streak_window", raw.get("window", 8))),
        reliable=bool(raw.get("reliable", False)),
        feature_count=int(raw.get("feature_count", raw.get("features", 3))),
        closed_signals=int(raw.get("closed_signals", raw.get("closed", 0))),
        confirm_ok=bool(raw.get("confirm_ok", False)),
        trigger_ok=bool(raw.get("trigger_ok", False)),
    )


def evaluate_csv(engine: ProbabilityEngine, csv_in: Path, csv_out: Path) -> dict[str, Any]:
    rows_out = 0
    go_count = 0
    with csv_in.open("r", encoding="utf-8", newline="") as fin, csv_out.open("w", encoding="utf-8", newline="") as fout:
        reader = csv.DictReader(fin)
        if not reader.fieldnames:
            raise ValueError("CSV sin encabezados")

        extra = [
            "p_pre",
            "p_cap",
            "p_oper",
            "gate",
            "gap_to_gate",
            "decision",
            "why_no",
            "boost",
            "anti_flat",
            "streak_density",
        ]
        fieldnames = list(reader.fieldnames) + [c for c in extra if c not in reader.fieldnames]
        writer = csv.DictWriter(fout, fieldnames=fieldnames)
        writer.writeheader()

        for row in reader:
            tick = tick_from_mapping(row)
            ev = engine.evaluate(tick)
            row.update(
                {
                    "p_pre": ev["p_pre"],
                    "p_cap": ev["p_cap"],
                    "p_oper": ev["p_oper"],
                    "gate": ev["gate"],
                    "gap_to_gate": ev["gap_to_gate"],
                    "decision": ev["decision"],
                    "why_no": ",".join(ev["why_no"]),
                    "boost": ev["boost"],
                    "anti_flat": ev["anti_flat"],
                    "streak_density": ev["streak_density"],
                }
            )
            writer.writerow(row)
            rows_out += 1
            if ev["decision"] == "GO":
                go_count += 1

    return {
        "rows": rows_out,
        "go_count": go_count,
        "go_rate": round((go_count / rows_out) if rows_out else 0.0, 4),
        "csv_out": str(csv_out),
    }


def evaluate_why_no_file(engine: ProbabilityEngine, src: Path, wins: int, window: int) -> dict[str, Any]:
    if not src.exists():
        return {"why_no_lines": 0, "parsed": 0, "go_count": 0, "go_rate": 0.0, "examples": [], "warning": f"archivo_no_encontrado:{src}"}
    lines = src.read_text(encoding="utf-8", errors="ignore").splitlines()
    total = 0
    go_count = 0
    parsed_count = 0
    examples: list[dict[str, Any]] = []

    for ln in lines:
        if "WHY-NO:" not in ln:
            continue
        total += 1
        parsed = parse_why_no_line(ln)
        tick = TickInput(
            p_raw=float(parsed["p_raw"]),
            streak_wins=wins,
            streak_window=window,
            reliable=bool(parsed["reliable"]),
            feature_count=int(parsed["feature_count"]),
            closed_signals=int(parsed["closed_signals"]),
            confirm_ok=bool(parsed["confirm_ok"]),
            trigger_ok=bool(parsed["trigger_ok"]),
        )
        ev = engine.evaluate(tick)
        parsed_count += 1
        if ev["decision"] == "GO":
            go_count += 1
        if len(examples) < 5:
            examples.append({"line": ln.strip(), "evaluation": ev})

    return {
        "why_no_lines": total,
        "parsed": parsed_count,
        "go_count": go_count,
        "go_rate": round((go_count / parsed_count) if parsed_count else 0.0, 4),
        "examples": examples,
    }


def _sample_ticks() -> list[TickInput]:
    return [
        TickInput(0.21, 5, 8, False, 3, 0, False, False),
        TickInput(0.43, 6, 8, False, 3, 10, True, False),
        TickInput(0.49, 7, 8, False, 3, 20, True, True),
        TickInput(0.58, 6, 8, True, 6, 80, True, True),
    ]


def _simulate_grid(engine: ProbabilityEngine, reliable: bool, feature_count: int, confirm_ok: bool, trigger_ok: bool) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for step in range(10, 71, 5):
        tick = TickInput(
            p_raw=step / 100.0,
            streak_wins=6,
            streak_window=8,
            reliable=reliable,
            feature_count=feature_count,
            closed_signals=80,
            confirm_ok=confirm_ok,
            trigger_ok=trigger_ok,
        )
        ev = engine.evaluate(tick)
        out.append(
            {
                "p_raw": tick.p_raw,
                "p_pre": ev["p_pre"],
                "p_cap": ev["p_cap"],
                "p_oper": ev["p_oper"],
                "decision": ev["decision"],
                "gap_to_gate": ev["gap_to_gate"],
            }
        )
    return out


def _tick_from_args(args: argparse.Namespace) -> TickInput:
    return TickInput(
        p_raw=float(args.p_raw),
        streak_wins=int(args.wins),
        streak_window=int(args.window),
        reliable=bool(args.reliable),
        feature_count=int(args.feature_count),
        closed_signals=int(args.closed),
        confirm_ok=bool(args.confirm_ok),
        trigger_ok=bool(args.trigger_ok),
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="5R6M master engine operativo")

    ap.add_argument("--config", type=str, default="", help="Ruta JSON con EngineConfig")

    ap.add_argument("--p-raw", type=float, default=None)
    ap.add_argument("--wins", type=int, default=0)
    ap.add_argument("--window", type=int, default=8)
    ap.add_argument("--reliable", action="store_true")
    ap.add_argument("--feature-count", type=int, default=3)
    ap.add_argument("--closed", type=int, default=0)
    ap.add_argument("--confirm-ok", action="store_true")
    ap.add_argument("--trigger-ok", action="store_true")

    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--simulate-grid", action="store_true")
    ap.add_argument("--from-why-no-line", type=str, default="")
    ap.add_argument("--tick-json", type=str, default="")

    ap.add_argument("--batch-csv-in", type=str, default="", help="CSV de entrada para evaluación batch")
    ap.add_argument("--batch-csv-out", type=str, default="", help="CSV de salida con columnas de evaluación")
    ap.add_argument("--why-no-file", type=str, default="", help="Archivo de log con líneas WHY-NO para replay")

    args = ap.parse_args()

    cfg = EngineConfig()
    if args.config:
        cfg = EngineConfig.from_json_file(Path(args.config))
    engine = ProbabilityEngine(cfg)

    if args.self_test:
        print(json.dumps([engine.evaluate(t) for t in _sample_ticks()], ensure_ascii=False, indent=2))
        return 0

    if args.simulate_grid:
        print(json.dumps(_simulate_grid(engine, args.reliable, args.feature_count, args.confirm_ok, args.trigger_ok), ensure_ascii=False, indent=2))
        return 0

    if args.from_why_no_line:
        parsed = parse_why_no_line(args.from_why_no_line)
        tick = TickInput(
            p_raw=float(parsed["p_raw"]),
            streak_wins=int(args.wins),
            streak_window=int(args.window),
            reliable=bool(parsed["reliable"]),
            feature_count=int(parsed["feature_count"]),
            closed_signals=int(parsed["closed_signals"]),
            confirm_ok=bool(parsed["confirm_ok"]),
            trigger_ok=bool(parsed["trigger_ok"]),
        )
        print(json.dumps({"parsed": parsed, "evaluation": engine.evaluate(tick)}, ensure_ascii=False, indent=2))
        return 0

    if args.tick_json:
        tick = tick_from_mapping(json.loads(args.tick_json))
        print(json.dumps(engine.evaluate(tick), ensure_ascii=False, indent=2))
        return 0

    if args.batch_csv_in:
        csv_in = Path(args.batch_csv_in)
        csv_out = Path(args.batch_csv_out) if args.batch_csv_out else csv_in.with_name(csv_in.stem + "_evaluado.csv")
        print(json.dumps(evaluate_csv(engine, csv_in, csv_out), ensure_ascii=False, indent=2))
        return 0

    if args.why_no_file:
        print(json.dumps(evaluate_why_no_file(engine, Path(args.why_no_file), args.wins, args.window), ensure_ascii=False, indent=2))
        return 0

    if args.p_raw is None:
        ap.error("Debes pasar un modo: --p-raw, --tick-json, --from-why-no-line, --simulate-grid, --batch-csv-in, --why-no-file o --self-test")

    print(json.dumps(engine.evaluate(_tick_from_args(args)), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
