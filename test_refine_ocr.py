#!/usr/bin/env python3
"""
Script de Refinamento OCR - Tenta identificar documentos com estratégias avançadas
Uso: python test_refine_ocr.py
"""

import os
import sys
from pathlib import Path

# Setup
project_root = Path(__file__).resolve().parent
os.environ["TESSDATA_PREFIX"] = str(project_root / "tessdata")
sys.path.insert(0, str(project_root))

from main import (
    extract_text_from_pdf,
    classify_document,
    extract_document_data,
    find_tesseract_path,
    find_poppler_path,
    normalize_ocr_text,
    setup_logging,
    DOC_TYPE_SIGNATURES,
    COMPILED_SIGNATURES
)


def analyze_unidentified_pdf(pdf_path: Path, tesseract_path: str, poppler_path: str | None, logger):
    """Analisa um PDF não-identificado com estratégias de refinamento."""
    print(f"\n{'='*70}")
    print(f"Analisando: {pdf_path.name}")
    print(f"{'='*70}")
    
    try:
        # Estratégia 1: OCR padrão (PSM 6)
        text1, doc_type1 = extract_text_from_pdf(pdf_path, tesseract_path, poppler_path, logger)
        normalized1 = normalize_ocr_text(text1)
        classified1 = classify_document(normalized1)
        
        print(f"\nEstratégia 1 (PSM 6, DPI 300):")
        print(f"  Tipo: {classified1 or 'NÃO IDENTIFICADO'}")
        print(f"  Tamanho OCR: {len(text1)} chars")
        
        # Mostrar primeiros 300 chars
        print(f"\n  Primeiros 300 chars do OCR:")
        print(f"  {'-'*66}")
        preview = text1[:300].replace('\n', ' ')
        print(f"  {preview}")
        print(f"  {'-'*66}")
        
        # Análise de padrões encontrados
        print(f"\n  Padrões encontrados:")
        for doc_type, compiled_regex in COMPILED_SIGNATURES.items():
            required_patterns = compiled_regex["required"]
            all_required = all(pattern.search(normalized1) for pattern in required_patterns)
            
            if all_required:
                print(f"    ✓ {doc_type}: MATCH!")
            elif any(pattern.search(normalized1) for pattern in required_patterns):
                matched = sum(1 for p in required_patterns if p.search(normalized1))
                print(f"    ◐ {doc_type}: Parcial ({matched}/{len(required_patterns)})")
        
        # Estatísticas de OCR
        alpha_count = sum(1 for c in text1 if c.isalpha())
        quality = (alpha_count / len(text1) * 100) if len(text1) > 0 else 0
        print(f"\n  Qualidade OCR: {quality:.1f}% (caracteres alfabéticos)")
        
        # Se baixa qualidade ou não identificado, sugerir próximos passos
        if quality < 30:
            print(f"\n  ⚠ AVISO: Qualidade muito baixa! Possível documento manuscrito ou scan ruim.")
            print(f"  Sugestão: Tentar HiRes (DPI 450) para melhor detecção de texto manuscrito.")
        
        if not classified1:
            print(f"\n  • Nenhum padrão obrigatório encontrado")
            print(f"  • Possível: Documento não-RH, Notes Fiscal, ou formulário customizado")
        
    except Exception as e:
        err_msg = str(e)
        if "PDFPageCountError" in err_msg or "Couldn't read xref table" in err_msg:
            print("  ERRO ao processar: PDF corrompido ou inválido (falha ao contar páginas).")
            return

        print(f"  ERRO ao processar: {e}")


def main():
    logger = setup_logging()
    
    # PDFs não-identificados para análise
    test_dir = project_root / "TEST PDFs"
    unidentified = [
        "doc20250616171700.pdf",
        "doc20251224103853.pdf",
        "doc20251224104041.pdf",
        "GUILHERME OLIVEIRA.pdf",
        "MATEUS FERREIRA.pdf",
    ]
    
    tesseract_path = find_tesseract_path()
    poppler_path = find_poppler_path()
    
    print(f"Tesseract: {tesseract_path}")
    print(f"Poppler: {poppler_path or 'Não detectado'}")
    print(f"Teste Dir: {test_dir}")
    
    # Analisar cada PDF
    for unn in unidentified:
        pdf_path = test_dir / unn
        if pdf_path.exists():
            analyze_unidentified_pdf(pdf_path, tesseract_path, poppler_path, logger)
        else:
            print(f"\n⚠ PDF não encontrado: {pdf_path}")
    
    print(f"\n{'='*70}")
    print("Fim da análise")
    print(f"{'='*70}")


if __name__ == "__main__":
    main()
