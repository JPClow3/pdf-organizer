#!/usr/bin/env python3
"""Teste real nao destrutivo para um PDF do scanner."""

from pathlib import Path
import logging
import sys

from main import (
    validate_pdf_integrity,
    extract_text_from_pdf_adaptive,
    get_classification_confidence,
    extract_document_data,
    extract_fallback_data,
    find_tesseract_path,
    find_poppler_path,
)

logging.basicConfig(level=logging.ERROR)
logger = logging.getLogger("test_real")


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

    is_valid, is_transient, msg = validate_pdf_integrity(pdf_path, poppler_path, logger)
    print(f"Integridade: valid={is_valid} transient={is_transient} msg={msg}")

    text, doc_type = extract_text_from_pdf_adaptive(pdf_path, tesseract_path, poppler_path, logger)
    if doc_type is None:
        data = extract_fallback_data(text)
        doc_type = "GEN"
    else:
        data = extract_document_data(text, doc_type)

    confidence = get_classification_confidence(text, None if doc_type == "GEN" else doc_type)

    print("TESTE REAL")
    print(f"Arquivo: {pdf_path.name}")
    print(f"Tipo: {doc_type}")
    print(f"Nome: {data.get('name')}")
    print(f"Periodo: {data.get('period')}")
    print(f"Confidence: {confidence:.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
