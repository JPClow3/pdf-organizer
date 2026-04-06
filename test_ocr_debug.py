#!/usr/bin/env python3
"""
Script de debug para analisar OCR de PDFs específicos
Útil para diagnosticar por que documentos não estão sendo identificados
"""

import os
import sys
from pathlib import Path

# Adicionar projeto ao path
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

# Configurar TESSDATA_PREFIX
os.environ["TESSDATA_PREFIX"] = str(PROJECT_ROOT / "tessdata")

from main import (
    extract_text_from_pdf,
    classify_document,
    extract_document_data,
    find_tesseract_path,
    find_poppler_path,
    normalize_ocr_text,
    setup_logging,
    DOC_TYPE_SIGNATURES,
)

def analyze_pdf(pdf_path: Path):
    """Analisa um PDF e mostra detalhes do OCR e classificação."""
    logger = setup_logging()
    
    print(f"\n{'='*70}")
    print(f"Analisando: {pdf_path.name}")
    print(f"{'='*70}\n")
    
    try:
        tesseract_path = find_tesseract_path()
        poppler_path = find_poppler_path()
        
        # Extrair texto
        text, doc_type = extract_text_from_pdf(pdf_path, tesseract_path, poppler_path, logger)
        
        print(f"Tipo detectado: {doc_type or 'NÃO IDENTIFICADO'}")
        print(f"Tamanho do OCR: {len(text)} caracteres\n")
        
        # Mostrar OCR normalizado (primeiros 500 chars)
        normalized = normalize_ocr_text(text)
        print(f"OCR (primeiros 500 chars):")
        print(f"{'-'*70}")
        print(normalized[:500])
        print(f"{'-'*70}\n")
        
        # Mostrar matching de signatures
        print(f"Análise de Signatures:")
        print(f"{'-'*70}")
        for sig_type, sig_config in DOC_TYPE_SIGNATURES.items():
            required_matches = []
            optional_matches = []
            
            for pattern in sig_config.get("required", []):
                import re
                if re.search(pattern, normalized, re.IGNORECASE):
                    required_matches.append(pattern[:40])
            
            for pattern in sig_config.get("optional", []):
                import re
                if re.search(pattern, normalized, re.IGNORECASE):
                    optional_matches.append(pattern[:40])
            
            status = "✓" if len(required_matches) == len(sig_config.get("required", [])) else "✗"
            print(f"\n{status} {sig_type}:")
            print(f"  Obrigatórias ({len(required_matches)}/{len(sig_config.get('required', []))}): {required_matches}")
            if optional_matches:
                print(f"  Opcionais ({len(optional_matches)}): {optional_matches}")
        
        # Extrair dados
        if doc_type:
            print(f"\n{'='*70}")
            data = extract_document_data(normalized, doc_type)
            print(f"Dados extraídos:")
            print(f"  Nome: {data.get('name', 'ERRO')}")
            print(f"  Período: {data.get('period', 'ERRO')}")
            
    except Exception as e:
        err_msg = str(e)
        if "PDFPageCountError" in err_msg or "Couldn't read xref table" in err_msg:
            print("ERRO: PDF corrompido ou inválido (não foi possível ler a estrutura do arquivo).")
            return

        print(f"ERRO: {e}")

if __name__ == "__main__":
    test_dir = PROJECT_ROOT / "TEST PDFs"
    
    # Analisar um PDF não-identificado
    unidentified = [
        "doc20251224103834.pdf",
        "GUILHERME OLIVEIRA.pdf",
        "MATEUS FERREIRA.pdf",
    ]
    
    for pdf_name in unidentified:
        pdf_path = test_dir / pdf_name
        if pdf_path.exists():
            analyze_pdf(pdf_path)
        else:
            print(f"PDF não encontrado: {pdf_path}")
