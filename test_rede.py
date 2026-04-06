#!/usr/bin/env python3
"""Teste simples com caminho correto"""
import os
import sys
from pathlib import Path


def resolve_paths() -> tuple[Path, Path]:
    """Resolve diretórios de teste e projeto (local primeiro, rede como fallback)."""
    local_project = Path(__file__).resolve().parent
    local_test_dir = local_project / "TEST PDFs"
    if local_test_dir.exists():
        return local_test_dir, local_project

    network_test_dir = Path(r"\\10.65.3.44\rede\TECNOLOGIA DA INFORMACAO\AUTOMAÇÂO PDF RH\TEST PDFs")
    return network_test_dir, network_test_dir.parent


test_dir, project_root = resolve_paths()

os.environ["TESSDATA_PREFIX"] = str(project_root / "tessdata")
sys.path.insert(0, str(project_root))

try:
    from main import extract_text_from_pdf, find_tesseract_path, find_poppler_path, setup_logging, classify_document
    
    logger = setup_logging()
    test_pdf = test_dir / "doc20251224103834.pdf"
    
    print(f"Testando: {test_pdf}")
    print(f"Existe: {test_pdf.exists()}")
    
    if test_pdf.exists():
        tesseract_path = find_tesseract_path()
        poppler_path = find_poppler_path()
        
        text, doc_type = extract_text_from_pdf(test_pdf, tesseract_path, poppler_path, logger)
        
        print(f"\nTipo (do extract): {doc_type}")
        
        # Tentar classificar novamente
        from main import normalize_ocr_text
        normalized = normalize_ocr_text(text)
        classified = classify_document(normalized)
        print(f"Tipo (do classify): {classified}")
        
        print(f"Tamanho OCR: {len(text)} chars")
        print(f"\nPrimeiros 400 chars:")
        print(text[:400])

except Exception as e:
    print(f"ERRO: {e}")
    import traceback
    traceback.print_exc()
