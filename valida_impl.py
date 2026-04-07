#!/usr/bin/env python3
"""Script de validacao das implementacoes OCR."""

from main import EXTRACTORS, MAX_PAGES_TO_OCR, DOC_TYPE_SIGNATURES

print('VALIDANDO IMPLEMENTACOES:')
print()

# 1. Validar MAX_PAGES_TO_OCR
print(f'[1] MAX_PAGES_TO_OCR = {MAX_PAGES_TO_OCR}')
if MAX_PAGES_TO_OCR is None:
    print('   [OK] Configurado para ler TODAS as paginas')
else:
    print(f'   [AVISO] Configurado para {MAX_PAGES_TO_OCR} paginas')
print()

# 2. Validar tipos de documento
print('[2] Tipos de documentos registrados:')
if 'ADVERTENCIA_ESCRITA' in DOC_TYPE_SIGNATURES:
    print('   [OK] ADVERTENCIA_ESCRITA encontrado')
else:
    print('   [ERRO] ADVERTENCIA_ESCRITA NAO encontrado')
print(f'   Total: {len(DOC_TYPE_SIGNATURES)} tipos')
print()

# 3. Validar EXTRACTORS
print('[3] Funcoes registradas em EXTRACTORS (tipos de documento):')
target_docs = ['ADVERTENCIA_ESCRITA', 'FMM', 'NF']
for doc_type in target_docs:
    if doc_type in EXTRACTORS:
        print(f'   [OK] {doc_type}')
    else:
        print(f'   [ERRO] {doc_type} FALTANDO')
print(f'   Total: {len(EXTRACTORS)} tipos com extractores')
print()

# 4. Testar extract_advertencia_escrita_data
if 'ADVERTENCIA_ESCRITA' in EXTRACTORS:
    func = EXTRACTORS['ADVERTENCIA_ESCRITA']
    test_text = 'ADVERTENCIA ESCRITA\nCOLABORADOR: JOAO DA SILVA\nDATA: 10/03/2026'
    result = func(test_text)
    print('[4] Teste extract_advertencia_escrita_data:')
    print(f'   Nome extraido: {result.get("name")}')
    print(f'   Periodo: {result.get("period")}')
    if result.get("name"):
        print('   [OK] Extracao funcionando')
    else:
        print('   [AVISO] Nome nao extraido')
print()

# 5. Testar extract_fmm_data (data final)
if 'FMM' in EXTRACTORS:
    func = EXTRACTORS['FMM']
    test_text = '''FECHAMENTO MENSAL MOTORISTA
    NOME: JOAO SILVA
    NUMERO DE FECHAMENTO: 123
    DATA FINAL: 31/03/2026
    '''
    result = func(test_text)
    print('[5] Teste extract_fmm_data (data final):')
    print(f'   Nome: {result.get("name")}')
    print(f'   Periodo (data final): {result.get("period")}')
    print(f'   Numero de fechamento: {result.get("closing_number")}')
    if result.get("period"):
        print('   [OK] Data sendo extraida')
print()

print('[RESULTADO FINAL] VALIDACOES COMPLETADAS!')
