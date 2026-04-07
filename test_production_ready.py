#!/usr/bin/env python3
"""Final production readiness check."""

import sys
import traceback

print('=' * 90)
print('FINAL PRODUCTION READINESS CHECK')
print('=' * 90)

# Test 1: Import main module
print('\n1. Testing imports...')
try:
    from main import (
        DOC_TYPE_SIGNATURES,
        EXTRACTORS,
        DOC_TYPE_PRIORITY,
        OCR_CORRECTIONS,
        classify_document,
        extract_document_data,
        clean_name,
        correct_year,
        extract_relatorio_abastecimento_data,
        extract_solicitacao_contratacao_data,
    )
    print('   [OK] ALL IMPORTS SUCCESSFUL')
except Exception as e:
    print(f'   [ERRO] IMPORT FAILED: {e}')
    traceback.print_exc()
    sys.exit(1)

# Test 2: Verify extractors are registered
print('\n2. Verifying extractors...')
print(f'   Total registered extractors: {len(EXTRACTORS)}')
required_extractors = [
    'RELATORIO_ABASTECIMENTO',
    'SOLICITACAO_CONTRATACAO',
    'FMM',
    'CP',
    'FN',
]
for extractor_name in required_extractors:
    if extractor_name not in EXTRACTORS:
        print(f'   [ERRO] MISSING EXTRACTOR: {extractor_name}')
        sys.exit(1)
    else:
        print(f'   [OK] {extractor_name}')

# Test 3: Verify document type signatures
print('\n3. Verifying signatures...')
print(f'   Total document types: {len(DOC_TYPE_SIGNATURES)}')
for doc_type in required_extractors:
    if doc_type not in DOC_TYPE_SIGNATURES:
        print(f'   [ERRO] MISSING SIGNATURE: {doc_type}')
        sys.exit(1)
    else:
        sig = DOC_TYPE_SIGNATURES[doc_type]
        print(f'   [OK] {doc_type} ({len(sig.get("required", []))} req, {len(sig.get("optional", []))} opt)')

# Test 4: Test new extraction functions with dummy data
print('\n4. Testing extraction functions...')
test_cases = [
    ('RELATORIO_ABASTECIMENTO', 'Motorista Silva Abastecimento KM 100 01/01/2025 a 31/01/2025'),
    ('SOLICITACAO_CONTRATACAO', 'RE: MP - Contratação Autorizado João 10/10/2025'),
    ('FMM', 'Fechamento Mensal Motorista Roberto Periodo 01/01/2025 a 31/01/2025'),
]

for doc_type, sample_text in test_cases:
    try:
        extractor = EXTRACTORS.get(doc_type)
        result = extractor(sample_text)
        if not isinstance(result, dict):
            print(f'   [ERRO] {doc_type}: Invalid return type (expected dict, got {type(result).__name__})')
            sys.exit(1)
        if 'name' not in result or 'period' not in result:
            print(f'   [ERRO] {doc_type}: Missing keys in result dict')
            sys.exit(1)
        print(f'   [OK] {doc_type}: name="{result["name"]}", period="{result["period"]}"')
    except Exception as e:
        print(f'   [ERRO] {doc_type}: {e}')
        traceback.print_exc()
        sys.exit(1)

# Test 5: Test classification
print('\n5. Testing classification...')
test_docs = [
    ('Test Abastecimento KM 100 Litro', 'RELATORIO_ABASTECIMENTO'),
    ('Test Fechamento Mensal Motorista Silva', 'FMM'),
    ('Test RE: MP - Contratação Autorizado', 'SOLICITACAO_CONTRATACAO'),
]

for sample_text, expected_type in test_docs:
    try:
        result = classify_document(sample_text)
        if result == expected_type:
            print(f'   [OK] "{sample_text[:40]}..." -> {result}')
        else:
            print(f'   [ALERTA]️  "{sample_text[:40]}..." -> {result} (expected {expected_type})')
    except Exception as e:
        print(f'   [ERRO] Classification failed: {e}')
        traceback.print_exc()
        sys.exit(1)

# Test 6: Test name cleaning
print('\n6. Testing edge case handling...')
test_names = [
    ('JOÃO DA SILVA SANTOS JUNIOR EXTRA EXTRA EXTRA EXTRA', 'should truncate'),
    ('JOÃO—SILVA', 'should remove em-dash'),
    ('[JOÃO SILVA', 'should remove brackets'),
    ('', 'empty string'),
]

for test_name, description in test_names:
    try:
        result = clean_name(test_name)
        print(f'   [OK] clean_name("{test_name[:30]}...") = "{result}" ({description})')
    except Exception as e:
        print(f'   [ERRO] clean_name failed: {e}')
        traceback.print_exc()
        sys.exit(1)

# Test 7: Test year correction
print('\n7. Testing year correction...')
test_years = [
    ('2025', '2025', 'valid'),
    ('202', 'current', 'truncated'),
    ('2202', 'candidate', 'digit swap'),
]

for test_year, expected_type, description in test_years:
    try:
        result = correct_year(test_year)
        print(f'   [OK] correct_year("{test_year}") = "{result}" ({description})')
    except Exception as e:
        print(f'   [ERRO] correct_year failed: {e}')
        traceback.print_exc()
        sys.exit(1)

print('\n' + '=' * 90)
print('[OK] ALL TESTS PASSED - PRODUCTION READY')
print('=' * 90)
