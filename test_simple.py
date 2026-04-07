#!/usr/bin/env python3
"""Validação rápida das implementações."""
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))

try:
    from main import MAX_PAGES_TO_OCR, EXTRACTORS, DOC_TYPE_SIGNATURES
    
    results = []
    
    # Teste 1: MAX_PAGES_TO_OCR
    if MAX_PAGES_TO_OCR is None:
        results.append("PASS: MAX_PAGES_TO_OCR is None")
    else:
        results.append(f"FAIL: MAX_PAGES_TO_OCR = {MAX_PAGES_TO_OCR}")
    
    # Teste 2: ADVERTENCIA_ESCRITA
    if 'ADVERTENCIA_ESCRITA' in DOC_TYPE_SIGNATURES:
        results.append("PASS: ADVERTENCIA_ESCRITA type registered")
    else:
        results.append("FAIL: ADVERTENCIA_ESCRITA type NOT registered")
    
    # Teste 3: Tipos extratores
    doc_types_needed = ['ADVERTENCIA_ESCRITA', 'FMM', 'NF']
    for doc_type in doc_types_needed:
        if doc_type in EXTRACTORS:
            results.append(f"PASS: {doc_type} extractor registered")
        else:
            results.append(f"FAIL: {doc_type} extractor NOT registered")
    
    # Output
    for result in results:
        print(result)
        
    # Summary
    if all("PASS" in r for r in results):
        print("\nALL TESTS PASSED!")
        sys.exit(0)
    else:
        print("\nSOME TESTS FAILED!")
        sys.exit(1)
        
except Exception as e:
    print(f"ERROR: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
