#!/usr/bin/env python3
"""Validacao rapida das implementacoes OCR."""

from main import EXTRACTORS, MAX_PAGES_TO_OCR, DOC_TYPE_SIGNATURES

print("VALIDANDO IMPLEMENTACOES")
print()

print(f"[1] MAX_PAGES_TO_OCR = {MAX_PAGES_TO_OCR}")
if MAX_PAGES_TO_OCR is None:
    print("    [OK] configurado para ler todas as paginas")
else:
    print(f"    [WARN] limitado para {MAX_PAGES_TO_OCR} paginas")
print()

print("[2] Tipos de documento registrados")
for required_type in ["ADVERTENCIA_ESCRITA", "FMM", "NF"]:
    if required_type in DOC_TYPE_SIGNATURES:
        print(f"    [OK] {required_type} em DOC_TYPE_SIGNATURES")
    else:
        print(f"    [ERRO] {required_type} ausente em DOC_TYPE_SIGNATURES")
print(f"    Total tipos: {len(DOC_TYPE_SIGNATURES)}")
print()

print("[3] Tipos com extractor")
for required_type in ["ADVERTENCIA_ESCRITA", "FMM", "NF"]:
    if required_type in EXTRACTORS:
        print(f"    [OK] {required_type} em EXTRACTORS")
    else:
        print(f"    [ERRO] {required_type} ausente em EXTRACTORS")
print(f"    Total extractors: {len(EXTRACTORS)}")
print()

print("[4] Teste funcional extractor ADVERTENCIA_ESCRITA")
if "ADVERTENCIA_ESCRITA" in EXTRACTORS:
    result = EXTRACTORS["ADVERTENCIA_ESCRITA"](
        "ADVERTENCIA ESCRITA\nCOLABORADOR: JOAO DA SILVA\nDATA: 10/03/2026"
    )
    print(f"    Nome: {result.get('name')}")
    print(f"    Periodo: {result.get('period')}")

print()
print("[OK] VALIDACAO CONCLUIDA")
