#!/usr/bin/env python3
"""Teste simples de um PDF"""
import os
import sys
from pathlib import Path

os.environ["TESSDATA_PREFIX"] = str(Path(__file__).parent / "tessdata")


def resolve_test_dir() -> Path:
    """Resolve pasta de testes priorizando workspace local e fallback para rede."""
    local_dir = Path(__file__).parent / "TEST PDFs"
    if local_dir.exists():
        return local_dir

    return Path(r"\\10.65.3.44\rede\TECNOLOGIA DA INFORMACAO\AUTOMAÇÂO PDF RH\TEST PDFs")


def resolve_test_pdf(test_dir: Path) -> Path | None:
    """Escolhe um PDF existente para o teste sem depender de um nome fixo."""
    preferred = test_dir / "doc20251224103834.pdf"
    if preferred.exists():
        return preferred

    candidates = sorted(test_dir.glob("*.pdf"), key=lambda item: item.name.lower())
    if candidates:
        return candidates[0]

    return None

try:
    from main import extract_text_from_pdf, find_tesseract_path, find_poppler_path, setup_logging
    
    logger = setup_logging()
    test_dir = resolve_test_dir()
    test_pdf = resolve_test_pdf(test_dir)
    
    if test_pdf is None:
        print(f"Nenhum PDF encontrado em: {test_dir}")
    else:
        print(f"Testando: {test_pdf}")
        print(f"Existe: {test_pdf.exists()}")

        tesseract_path = find_tesseract_path()
        poppler_path = find_poppler_path()
        
        text, doc_type = extract_text_from_pdf(test_pdf, tesseract_path, poppler_path, logger)
        
        print(f"\nTipo: {doc_type}")
        print(f"Tamanho OCR: {len(text)} chars")
        print(f"\nPrimeiros 300 chars:")
        print(text[:300])
        
except Exception as e:
    print(f"ERRO: {e}")
    import traceback
    traceback.print_exc()
