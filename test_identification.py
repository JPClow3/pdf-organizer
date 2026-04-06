#!/usr/bin/env python3
"""Quick identification test on sample files from TEST PDFs."""

from pathlib import Path
from main import (
    extract_text_from_pdf, classify_document, normalize_ocr_text,
    find_tesseract_path, find_poppler_path, setup_logging,
    get_classification_confidence
)

logger = setup_logging()
tesseract = find_tesseract_path()
poppler = find_poppler_path()
test_dir = Path("TEST PDFs")

# Get sample files
files = list(test_dir.glob("*.pdf"))
samples = [
    "ABERTURA DE VAGA - ASSISTENTE DE CONTROLE DE JORNADA RV.pdf",
    "DECLARACAO DE ULTIMO DIA TRABALHADO 18-07-2025.pdf",
    "DUT JACIELE.pdf",
    "PAPELETA - SEM NOME.pdf",
]

print("=" * 80)
print("DOCUMENT TYPE IDENTIFICATION TEST")
print("=" * 80)
print()

for sample_name in samples:
    sample_path = test_dir / sample_name
    if not sample_path.exists():
        print(f"SKIP: {sample_name} (not found)")
        continue
    
    print(f"FILE: {sample_name}")
    print("-" * 80)
    
    try:
        text, doc_type = extract_text_from_pdf(sample_path, tesseract, poppler, logger)
        
        if doc_type:
            confidence = get_classification_confidence(text, doc_type)
            print(f"  Type: {doc_type}")
            print(f"  Confidence: {confidence:.1f}%")
            print(f"  Text sample: {text[:100]}...")
        else:
            print(f"  Type: NOT IDENTIFIED")
            print(f"  Text sample: {text[:100]}...")
        
    except Exception as e:
        print(f"  ERROR: {e}")
    
    print()

print("=" * 80)
print("IDENTIFICATION TEST COMPLETE")
print("=" * 80)
