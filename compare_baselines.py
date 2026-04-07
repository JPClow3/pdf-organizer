#!/usr/bin/env python3
"""Compara dois relatórios baseline/improved e aponta regressões."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _delta(new_val: float | int | None, old_val: float | int | None) -> float | None:
    if new_val is None or old_val is None:
        return None
    return round(float(new_val) - float(old_val), 2)


def main() -> int:
    parser = argparse.ArgumentParser(description="Compara dois relatórios de tuning")
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--improved", required=True)
    args = parser.parse_args()

    baseline = _load(Path(args.baseline))
    improved = _load(Path(args.improved))

    b = baseline["summary"]
    i = improved["summary"]

    report = {
        "identified_percent_delta": _delta(i.get("identified_percent"), b.get("identified_percent")),
        "name_found_percent_delta": _delta(i.get("name_found_percent"), b.get("name_found_percent")),
        "avg_confidence_delta": _delta(i.get("avg_confidence"), b.get("avg_confidence")),
        "avg_elapsed_seconds_delta": _delta(i.get("avg_elapsed_seconds"), b.get("avg_elapsed_seconds")),
        "doc_type_accuracy_percent_delta": _delta(i.get("doc_type_accuracy_percent"), b.get("doc_type_accuracy_percent")),
        "name_accuracy_percent_delta": _delta(i.get("name_accuracy_percent"), b.get("name_accuracy_percent")),
        "unidentified_delta": _delta(i.get("unidentified"), b.get("unidentified")),
        "type_without_name_delta": _delta(i.get("type_without_name"), b.get("type_without_name")),
    }

    baseline_records = {r["filename"]: r for r in baseline.get("records", [])}
    improved_records = {r["filename"]: r for r in improved.get("records", [])}

    regressions = []
    for name, b_rec in baseline_records.items():
        i_rec = improved_records.get(name)
        if not i_rec:
            continue
        if b_rec.get("doc_type_match") is True and i_rec.get("doc_type_match") is False:
            regressions.append({"filename": name, "kind": "doc_type"})
        if b_rec.get("name_match") is True and i_rec.get("name_match") is False:
            regressions.append({"filename": name, "kind": "name"})

    print("COMPARACAO")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"Regressoes detectadas: {len(regressions)}")
    if regressions:
        print(json.dumps(regressions[:20], ensure_ascii=False, indent=2))

    # Critério padrão de aceite para esta rodada
    ok = (
        (report["identified_percent_delta"] is None or report["identified_percent_delta"] >= 0)
        and (report["name_found_percent_delta"] is None or report["name_found_percent_delta"] >= 0)
        and len(regressions) == 0
    )

    if ok:
        print("STATUS: APROVADO")
        return 0

    print("STATUS: REPROVADO")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
