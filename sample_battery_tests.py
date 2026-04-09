#!/usr/bin/env python3
"""
Runs a reproducible battery against PDFs in sample/ without mutating them.

The script copies the sample corpus into a temporary directory, executes the
real processing path, and prints a JSON summary that is easy to diff over time.
"""

import io
import json
import logging
import shutil
import sys
import tempfile
from pathlib import Path

workspace = Path(__file__).parent
sys.path.insert(0, str(workspace))

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from main import (  # pylint: disable=wrong-import-position
    build_new_filename,
    configure_tesseract_command,
    find_poppler_path,
    find_tesseract_path,
    load_config,
    process_single_pdf,
)


def is_labeled_sample(path: Path) -> bool:
    return " - " in path.stem


def build_expected_signals(path: Path) -> dict[str, str | None]:
    if not is_labeled_sample(path):
        return {"doc_label": None, "date_hint": None}

    parts = [part.strip() for part in path.stem.split(" - ")]
    doc_label = parts[0] if len(parts) >= 1 else None
    date_hint = parts[-1] if len(parts) >= 3 else None
    return {"doc_label": doc_label, "date_hint": date_hint}


def main() -> int:
    sample_dir = workspace / "sample"
    if not sample_dir.is_dir():
        print(json.dumps({"error": f"sample folder not found: {sample_dir}"}, ensure_ascii=False, indent=2))
        return 1

    sample_files = sorted(sample_dir.glob("*.pdf"))
    if not sample_files:
        print(json.dumps({"error": "sample folder exists but contains no PDFs"}, ensure_ascii=False, indent=2))
        return 1

    config = load_config()
    tesseract_path = find_tesseract_path()
    poppler_path = find_poppler_path()
    configure_tesseract_command(tesseract_path)

    logger = logging.getLogger("sample-battery")
    logger.handlers.clear()
    logger.setLevel(logging.ERROR)
    logger.addHandler(logging.StreamHandler(sys.stderr))

    results: list[dict[str, object]] = []

    with tempfile.TemporaryDirectory(prefix="pdf-sample-battery-") as tmp:
        tmpdir = Path(tmp)
        for source_path in sample_files:
            tmp_path = tmpdir / source_path.name
            shutil.copy2(source_path, tmp_path)

            result = process_single_pdf(
                tmp_path,
                tesseract_path,
                poppler_path,
                tmpdir,
                logger,
                False,
                config.get("confidence_gate_enabled", True),
                config.get("confidence_thresholds", {}),
                float(config.get("confidence_baseline", 70.0)),
            )

            predicted = None
            if result.doc_type and result.extracted_name:
                predicted = build_new_filename(
                    result.doc_type,
                    result.extracted_name,
                    result.extracted_period,
                    result.extracted_closing_number,
                )

            expected_signals = build_expected_signals(source_path)
            doc_label_match = None
            if expected_signals["doc_label"] and predicted:
                doc_label_match = predicted.startswith(f"{expected_signals['doc_label']} - ")

            date_hint_match = None
            if expected_signals["date_hint"] and predicted:
                normalized_date_hint = expected_signals["date_hint"].replace(" (1)", "").replace(" (2)", "")
                date_hint_match = normalized_date_hint in predicted

            results.append(
                {
                    "source": source_path.name,
                    "status": result.status.value,
                    "doc_type": result.doc_type,
                    "confidence": round(result.confidence_score, 1),
                    "name": result.extracted_name,
                    "period": result.extracted_period,
                    "predicted": predicted,
                    "expected_doc_label": expected_signals["doc_label"],
                    "expected_date_hint": expected_signals["date_hint"],
                    "doc_label_match": doc_label_match,
                    "date_hint_match": date_hint_match,
                    "error": result.error_message,
                }
            )

    summary = {
        "total": len(results),
        "renamed": sum(1 for item in results if item["status"] == "RENOMEADO"),
        "unidentified": sum(1 for item in results if item["status"] == "NAO IDENTIFICADO"),
        "errors": sum(1 for item in results if item["status"] == "ERRO"),
        "placeholder_name": sum(1 for item in results if item["name"] == "NOME NAO LOCALIZADO"),
        "named_localized": sum(1 for item in results if item["name"] not in (None, "NOME NAO LOCALIZADO")),
        "labeled_samples": sum(1 for item in results if item["expected_doc_label"]),
        "labeled_doc_matches": sum(1 for item in results if item["doc_label_match"] is True),
        "labeled_date_matches": sum(1 for item in results if item["date_hint_match"] is True),
    }

    print(json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2))

    return 0 if summary["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
