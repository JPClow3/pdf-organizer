#!/usr/bin/env python3
"""Captura baseline de OCR/classificacao em lote real sem renomear arquivos."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import main as app


@dataclass
class EvalRecord:
    filename: str
    doc_type: str | None
    confidence: float
    extracted_name: str | None
    extracted_period: str | None
    identified: bool
    name_found: bool
    elapsed_seconds: float
    expected_doc_type: str | None = None
    expected_name: str | None = None
    doc_type_match: bool | None = None
    name_match: bool | None = None


def _load_truth(truth_csv: Path | None) -> dict[str, dict[str, str]]:
    if truth_csv is None or not truth_csv.exists():
        return {}

    truth: dict[str, dict[str, str]] = {}
    with truth_csv.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            filename = (row.get("filename") or "").strip()
            if not filename:
                continue
            truth[filename] = {
                "expected_doc_type": (row.get("expected_doc_type") or "").strip(),
                "expected_name": (row.get("expected_name") or "").strip(),
            }
    return truth


def _normalize_name(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(value.upper().split())


def _evaluate_one(
    pdf_path: Path,
    logger: logging.Logger,
    tesseract_path: str,
    poppler_path: str | None,
    truth_item: dict[str, str] | None,
) -> EvalRecord:
    start = time.perf_counter()

    text, doc_type = app.extract_text_from_pdf_adaptive(
        pdf_path,
        tesseract_path,
        poppler_path,
        logger,
    )

    extracted_name: str | None = None
    extracted_period: str | None = None

    if doc_type is None:
        fallback = app.extract_fallback_data(text)
        extracted_name = fallback.get("name")
        extracted_period = fallback.get("period")
        if extracted_name:
            doc_type = "GEN"
    else:
        if doc_type == "MBV":
            data = app.extract_mbv_data_from_rois(
                pdf_path,
                tesseract_path,
                poppler_path,
                logger,
            )
            extracted_name = data.get("name")
            extracted_period = data.get("period")
            if not extracted_name:
                textual_data = app.extract_mbv_data(text)
                extracted_name = textual_data.get("name")
                extracted_period = extracted_period or textual_data.get("period")
        else:
            data = app.extract_document_data(text, doc_type)
            extracted_name = data.get("name")
            extracted_period = data.get("period")

    confidence = app.get_classification_confidence(text, None if doc_type == "GEN" else doc_type)

    identified = doc_type is not None
    name_found = bool(extracted_name)
    elapsed = time.perf_counter() - start

    expected_doc_type = None
    expected_name = None
    doc_type_match = None
    name_match = None

    if truth_item:
        expected_doc_type = truth_item.get("expected_doc_type") or None
        expected_name = truth_item.get("expected_name") or None

        if expected_doc_type:
            doc_type_match = (doc_type or "") == expected_doc_type
        if expected_name:
            name_match = _normalize_name(extracted_name) == _normalize_name(expected_name)

    return EvalRecord(
        filename=pdf_path.name,
        doc_type=doc_type,
        confidence=confidence,
        extracted_name=extracted_name,
        extracted_period=extracted_period,
        identified=identified,
        name_found=name_found,
        elapsed_seconds=round(elapsed, 3),
        expected_doc_type=expected_doc_type,
        expected_name=expected_name,
        doc_type_match=doc_type_match,
        name_match=name_match,
    )


def _summarize(records: list[EvalRecord]) -> dict[str, Any]:
    total = len(records)
    identified = sum(1 for r in records if r.identified)
    name_found = sum(1 for r in records if r.name_found)
    unid = sum(1 for r in records if not r.identified)
    type_sem_nome = sum(1 for r in records if r.identified and not r.name_found)
    avg_conf = round(sum(r.confidence for r in records) / total, 2) if total else 0.0
    avg_time = round(sum(r.elapsed_seconds for r in records) / total, 3) if total else 0.0

    typed_matches = [r for r in records if r.doc_type_match is not None]
    name_matches = [r for r in records if r.name_match is not None]

    type_acc = round((sum(1 for r in typed_matches if r.doc_type_match) / len(typed_matches)) * 100, 2) if typed_matches else None
    name_acc = round((sum(1 for r in name_matches if r.name_match) / len(name_matches)) * 100, 2) if name_matches else None

    by_type: dict[str, int] = {}
    for r in records:
        key = r.doc_type or "NONE"
        by_type[key] = by_type.get(key, 0) + 1

    return {
        "total": total,
        "identified": identified,
        "identified_percent": round((identified / total) * 100, 2) if total else 0.0,
        "name_found": name_found,
        "name_found_percent": round((name_found / total) * 100, 2) if total else 0.0,
        "unidentified": unid,
        "type_without_name": type_sem_nome,
        "avg_confidence": avg_conf,
        "avg_elapsed_seconds": avg_time,
        "doc_type_accuracy_percent": type_acc,
        "name_accuracy_percent": name_acc,
        "by_type": by_type,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Captura baseline de lote real sem renomeacao")
    parser.add_argument("--dataset", required=True, help="Diretorio com PDFs")
    parser.add_argument("--output", default="baseline_reports", help="Diretorio de saida")
    parser.add_argument("--truth", default=None, help="CSV opcional: filename,expected_doc_type,expected_name")
    parser.add_argument("--limit", type=int, default=0, help="Limite de arquivos (0 = todos)")
    parser.add_argument("--tag", default="baseline", help="Tag do relatório")
    args = parser.parse_args()

    dataset_dir = Path(args.dataset)
    if not dataset_dir.exists() or not dataset_dir.is_dir():
        raise SystemExit(f"Dataset invalido: {dataset_dir}")

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("baseline")
    logger.setLevel(logging.ERROR)

    tesseract_path = app.find_tesseract_path()
    poppler_path = app.find_poppler_path()

    truth = _load_truth(Path(args.truth) if args.truth else None)

    pdfs = sorted(dataset_dir.glob("*.pdf"))
    if args.limit and args.limit > 0:
        pdfs = pdfs[: args.limit]

    if not pdfs:
        raise SystemExit("Nenhum PDF encontrado no dataset")

    records: list[EvalRecord] = []
    for idx, pdf_path in enumerate(pdfs, start=1):
        rec = _evaluate_one(pdf_path, logger, tesseract_path, poppler_path, truth.get(pdf_path.name))
        records.append(rec)
        print(f"[{idx}/{len(pdfs)}] {pdf_path.name} -> tipo={rec.doc_type} nome={'OK' if rec.name_found else 'MISS'} conf={rec.confidence:.1f}%")

    summary = _summarize(records)
    payload = {
        "meta": {
            "tag": args.tag,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "dataset": str(dataset_dir),
            "limit": args.limit,
            "truth_file": args.truth,
        },
        "summary": summary,
        "records": [asdict(r) for r in records],
    }

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = output_dir / f"{args.tag}_{stamp}.json"
    output_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\nRESUMO")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\nRelatorio salvo em: {output_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
