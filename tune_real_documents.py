#!/usr/bin/env python3
"""Executa tuning em rodadas com documentos reais, focado em assertividade."""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime
from pathlib import Path


ROUND_PROFILES = [
    {"name": "r01_control", "env": {}},
    {"name": "r02_preproc_robust", "env": {"OCR_TUNE_BLOCK_SIZE": "41", "OCR_TUNE_ADAPTIVE_C": "12"}},
    {"name": "r03_preproc_strong_denoise", "env": {"OCR_TUNE_MEDIAN": "5"}},
    {"name": "r04_title_hint_strict", "env": {"OCR_TUNE_TITLE_HINT_RATIO": "0.86"}},
    {"name": "r05_dpi_hires_bias", "env": {"OCR_TUNE_DPI": "320", "OCR_TUNE_DPI_HIRES": "500"}},
    {"name": "r06_upscale_high", "env": {"OCR_TUNE_MIN_WIDTH": "2400"}},
    {"name": "r07_clahe_high", "env": {"OCR_TUNE_CLAHE_CLIP": "3.0", "OCR_TUNE_CLAHE_GRID": "6,6"}},
    {"name": "r08_title_hint_relaxed", "env": {"OCR_TUNE_TITLE_HINT_RATIO": "0.82"}},
    {"name": "r09_balanced_combo", "env": {"OCR_TUNE_BLOCK_SIZE": "35", "OCR_TUNE_ADAPTIVE_C": "11", "OCR_TUNE_MEDIAN": "3", "OCR_TUNE_TITLE_HINT_RATIO": "0.85"}},
    {"name": "r10_final_candidate", "env": {"OCR_TUNE_BLOCK_SIZE": "37", "OCR_TUNE_ADAPTIVE_C": "11", "OCR_TUNE_TITLE_HINT_RATIO": "0.85", "OCR_TUNE_MIN_WIDTH": "2200"}},
]


def main() -> int:
    parser = argparse.ArgumentParser(description="Tuning por rodadas em lote real")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--output", default="baseline_reports")
    parser.add_argument("--python", default="python")
    parser.add_argument("--truth", default=None)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    dataset = Path(args.dataset)
    if not dataset.exists() or not dataset.is_dir():
        raise SystemExit(f"Dataset invalido: {dataset}")

    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)

    run_manifest = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "dataset": str(dataset),
        "rounds": [],
    }

    for profile in ROUND_PROFILES:
        round_name = profile["name"]
        print(f"\n=== {round_name} ===")

        env_cmd = []
        for key, value in profile["env"].items():
            env_cmd.extend(["--set-env", f"{key}={value}"])

        cmd = [
            args.python,
            "create_baseline.py",
            "--dataset",
            str(dataset),
            "--output",
            str(output),
            "--tag",
            round_name,
        ]
        if args.truth:
            cmd.extend(["--truth", args.truth])
        if args.limit > 0:
            cmd.extend(["--limit", str(args.limit)])

        # Executa com env temporário via PowerShell-style no Windows usando subprocess env
        run_env = None
        if profile["env"]:
            import os
            run_env = dict(os.environ)
            run_env.update(profile["env"])

        completed = subprocess.run(cmd, check=False, env=run_env)
        run_manifest["rounds"].append({
            "name": round_name,
            "env": profile["env"],
            "exit_code": completed.returncode,
        })

        if completed.returncode != 0:
            print(f"Round {round_name} falhou com exit_code={completed.returncode}")

    manifest_file = output / f"tuning_manifest_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    manifest_file.write_text(json.dumps(run_manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\nManifest salvo em: {manifest_file}")
    print("Tuning concluido")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
