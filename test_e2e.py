#!/usr/bin/env python3
"""Teste end-to-end nao destrutivo em PDF real."""

from pathlib import Path
import logging
import sys

from main import (
    extract_text_from_pdf_adaptive,
    classify_document,
    get_classification_confidence,
    extract_document_data,
    extract_fallback_data,
    find_tesseract_path,
    find_poppler_path,
)

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("test_e2e")


def main() -> int:
    scanner_dir = Path(r"G:\RH\EQUIPE RH\ARQUIVO\SCANNER")
    if not scanner_dir.exists():
        print("SKIP: scanner_dir nao encontrado")
        return 0

    pdfs = sorted(scanner_dir.glob("*.pdf"))
    if not pdfs:
        print("SKIP: nenhum PDF encontrado")
        return 0

    pdf_path = pdfs[0]
    tesseract_path = find_tesseract_path()
    poppler_path = find_poppler_path()

    text, doc_type = extract_text_from_pdf_adaptive(pdf_path, tesseract_path, poppler_path, logger)
    if doc_type is None:
        data = extract_fallback_data(text)
        doc_type = classify_document(text) or "GEN"
    else:
        data = extract_document_data(text, doc_type)

    confidence = get_classification_confidence(text, None if doc_type == "GEN" else doc_type)

    print("TESTE E2E")
    print(f"Arquivo: {pdf_path.name}")
    print(f"Tipo: {doc_type}")
    print(f"Nome: {data.get('name')}")
    print(f"Periodo: {data.get('period')}")
    print(f"Confidence: {confidence:.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
