"""
PDF Scanner - OCR Document Renamer
Le PDFs escaneados da pasta SCANNER, identifica tipo de documento
e nome do funcionario via OCR, e renomeia os arquivos.

Uso: python main.py
Configuracao: edite config.ini (criado automaticamente na primeira execucao)
"""

import argparse
import json
import os
import re
import unicodedata
import sys
import shutil
import logging
import threading
import configparser
import subprocess
import time
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, field
from enum import Enum
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from typing import Any

try:
    import cv2
    import numpy as np
    from PIL import Image
    import pytesseract
    from pdf2image import convert_from_path
    from tenacity import retry, stop_after_attempt, wait_exponential
except ImportError as e:
    print(f"Dependencia faltando: {e}")
    print("Instale com: pip install pytesseract pdf2image Pillow opencv-python numpy tenacity")
    sys.exit(1)


# =============================================================================
# CONFIGURACAO
# =============================================================================

# Caminhos relativos ao projeto (derivados da localizacao do script)
PROJECT_ROOT = Path(__file__).resolve().parent
TESSDATA_DIR = PROJECT_ROOT / "tessdata"
LOGS_DIR = PROJECT_ROOT / "logs"
CONFIG_FILE = PROJECT_ROOT / "config.ini"
DEFAULT_SCANNER_DIR = Path(r"G:\RH\EQUIPE RH\ARQUIVO\SCANNER")

# OCR
OCR_DPI = 300
OCR_DPI_HIRES = 450  # DPI alto para documentos manuscritos
OCR_LANG = "por"
MAX_PAGES_TO_OCR = 2
DEFAULT_WATCH_INTERVAL_SECONDS = 15
DEFAULT_FILE_STABILITY_SECONDS = 5.0
DEFAULT_FILE_STABILITY_CHECKS = 3
DEFAULT_DEFERRED_MAX_ATTEMPTS = 3
DEFAULT_DEFERRED_RETRY_COOLDOWN_SECONDS = 30
DEFAULT_WATCH_MAX_WORKERS = 3
DEFAULT_METRICS_LOG_EVERY_CYCLES = 4
DEFAULT_MIN_CONFIDENCE_BASELINE = 70.0
MONITOR_HEARTBEAT_FILE_NAME = ".monitor_heartbeat.json"
QUARANTINE_DIR_NAME = "_QUARENTENA"
REVIEW_DIR_NAME = "_REVISAO"

MBV_TEMPLATE_DIR = PROJECT_ROOT / "templates"
MBV_TEMPLATE_FILES = {
    0: "mbv_page1_blank.png",
    1: "mbv_page2_blank.png",
    2: "mbv_page3_blank.png",
}

# ROIs normalizados (x, y, w, h) em proporcao da pagina
MBV_FIELD_ROIS = {
    0: {
        "nome_titular": (0.18, 0.22, 0.58, 0.06),
        "cpf": (0.18, 0.29, 0.30, 0.05),
        "data_solicitacao": (0.64, 0.85, 0.22, 0.06),
        "checkbox_area": (0.08, 0.42, 0.22, 0.22),
    },
    1: {
        "nome_cargo_funcionario": (0.16, 0.73, 0.66, 0.08),
        "data": (0.64, 0.86, 0.22, 0.07),
        "checkbox_area": (0.07, 0.27, 0.22, 0.30),
    },
    2: {
        "empresa": (0.16, 0.76, 0.52, 0.07),
        "data": (0.64, 0.86, 0.22, 0.07),
    },
}

MBV_BLOCKED_NAME_TERMS = {
    "ANEXO",
    "TITULAR",
    "DEPENDENTES",
    "DEPENDENTE",
    "TELEFONE",
    "TEIEFONE",  # OCR typo
    "CONTRATANTE",
    "BENEFICIARIO",
    "BENEFICIÁRIO",
    "MOVIMENTACAO",
    "MOVIMENTAÇÃO",
    "NOME",
    "CARGO",
    "CPF",
    "RG",
    "DATA",
    "ASSINATURA",
    "DECLARO",
    "EU",
    "SEIS",
    "OPTO",
    "CONTINUIDADE",  # "NÃO OPTO..." phrases
    "PLANO",
    "CONDICAO",
    "CONDIÇÃO",
    "VINCULO",
    "VÍNCULO",
    "EXCLUSAO",  # "EXCLUSÃO POR INICIATIVA..."
    "EXCLUSÃO",
    "INCLUSAO",
    "INCLUSÃO",
    "ALTERACAO",  # "ALTERAÇÃO DO TIPO..."
    "ALTERAÇÃO",
    "INICIATIVA",
    "TIPO",
    "MEET",
    "ASMA",
    # Additional edge cases
    "BENETIZAANOS",  # OCR error from beneficiário/formulário artifact
    "NAOOPT",  # Malformed "NÃO OPTO"
    "PELA",  # Part of "NÃO OPTO PELA..."
    "RESPONSÁVEL",
    "RESPONSAVEL",
    "EMPRESA",
    "CONTRATADO",
    "PRESTADOR",  # Service provider
    "ASSINANTE",
    "SEGURADO",
    "FORMULARIO",
    "FORMULÁRIO",
    "DECLARACAO",
    "DECLARAÇÃO",
    "PERIODO",  # Should never be a name
    "PERÍODO",
    "RECUSA",
    "RENÚNCIA",
    "RENUNCIA",  # Renouncement
    "CANCELAMENTO",
    "SUSPENSAO",
    "SUSPENSÃO",
    "CORRETORA",  # Insurance broker
    "CORRETOR",
    "SOLICITACAO",
    "SOLICITAÇÃO",
    "VIGENCIA",  # Policy validity
    "VIGÊNCIA",
    "COBERTURA",
    "APÓLICE",
    "APOLICE",  # Policy
}

MBV_ALLOWED_SMALL_WORDS = {"DE", "DA", "DO", "DAS", "DOS", "E"}

# Termos comuns que indicam texto de formulario/rotulo e nao nome de pessoa.
COMMON_BLOCKED_NAME_TERMS = {
    "ANEXO",
    "FORMULARIO",
    "FORMULÁRIO",
    "DOCUMENTO",
    "DECLARACAO",
    "DECLARAÇÃO",
    "ASSINATURA",
    "ASSINADO",
    "DATA",
    "CPF",
    "RG",
    "CTPS",
    "PIS",
    "CARGO",
    "FUNCAO",
    "FUNÇÃO",
    "EMPRESA",
    "TELEFONE",
    "ENDERECO",
    "ENDEREÇO",
    "EMAIL",
    "CARIMBO",
    "PAGINA",
    "PÁGINA",
    "PROTOCOLO",
    "CONTINUIDADE",
    "PERIODO",
    "PERÍODO",
    "REFERENCIA",
    "REFERÊNCIA",
}

DOC_BLOCKED_NAME_TERMS = {
    "FMM": {
        "FECHAMENTO",
        "MOTORISTA",
        "CONDUTOR",
        "UNIDADE",
        "NEGOCIO",
        "NEGÓCIO",
        "RECEITAS",
        "ESTADIAS",
        "COMBUSTIVEL",
        "COMBUSTÍVEL",
        "PEDAGIO",
        "PEDÁGIO",
        "FRETE",
        "FILIAL",
        "RONDONOPOLIS",
        "RONDONÓPOLIS",
    },
    "CP": {
        "CARTAO",
        "CARTÃO",
        "PONTO",
        "JORNADA",
        "HORARIO",
        "HORÁRIO",
        "COMPETENCIA",
        "COMPETÊNCIA",
        "BANCO DE HORAS",
    },
    "FN": {
        "FOLHA",
        "NORMAL",
        "DIARIAS",
        "DIÁRIAS",
        "HORA EXTRA",
        "FROTA",
        "OPERACIONAL",
        "SALARIO",
        "SALÁRIO",
        "PROVENTOS",
        "DESCONTOS",
    },
    "AP": {
        "AVISO",
        "PREVIO",
        "PRÉVIO",
        "RESCISAO",
        "RESCISÃO",
        "EMPREGADOR",
        "EMPREGADO",
        "DISPENSA",
        "INDENIZADO",
        "TRABALHADO",
    },
    "ASO_ADMISSIONAL": {
        "ASO",
        "ADMISSIONAL",
        "EXAME",
        "SAUDE",
        "SAÚDE",
        "OCUPACIONAL",
    },
    "ASO_DEMISSIONAL": {
        "ASO",
        "DEMISSIONAL",
        "EXAME",
        "DEMISSAO",
        "DEMISSÃO",
        "OCUPACIONAL",
    },
    "ATESTADO_MEDICO": {
        "ATESTADO",
        "MEDICO",
        "MÉDICO",
        "PACIENTE",
        "AFASTAMENTO",
    },
    "CTPS": {
        "CTPS",
        "CARTEIRA",
        "TRABALHO",
        "PROFISSIONAL",
    },
    "CNH": {
        "CNH",
        "HABILITACAO",
        "HABILITAÇÃO",
        "CONDUTOR",
    },
    "CURRICULO": {
        "CURRICULO",
        "CURRÍCULO",
        "CURRICULUM",
        "VITAE",
    },
    "FGTS": {
        "FGTS",
        "FUNDO",
        "GARANTIA",
        "GUIA",
    },
    "HOLERITE": {
        "HOLERITE",
        "CONTRACHEQUE",
        "RECIBO",
        "PAGAMENTO",
    },
    "PPP": {
        "PPP",
        "PERFIL",
        "PROFISSIOGRAFICO",
        "PREVIDENCIARIO",
    },
    "AVALIACAO_MOTORISTA": {
        "AVALIACAO",
        "AVALIAÇÃO",
        "MOTORISTA",
        "MOTORISTA",
        "TESTE",
        "CONDUTOR",
    },
    "TESTE_PRATICO": {
        "TESTE",
        "PRATICO",
        "PRÁTICO",
        "CONDUTOR",
        "MOTORISTA",
    },
    "TESTE_CONHECIMENTOS_GERAIS": {
        "TESTE",
        "CONHECIMENTOS",
        "GERAIS",
        "QUESTIONARIO",
        "QUESTIONÁRIO",
    },
    "TREINAMENTO_DIRECAO_DEFENSIVA": {
        "TREINAMENTO",
        "DIRECAO",
        "DIREÇÃO",
        "DEFENSIVA",
        "SEGURANCA",
        "SEGURANÇA",
    },
    "PAPELETA_CONTROLE_JORNADA": {
        "PAPELETA",
        "CONTROLE",
        "JORNADA",
        "PONTO",
        "HORARIO",
        "HORÁRIO",
    },
    "QUESTIONARIO_ACOLHIMENTO": {
        "QUESTIONARIO",
        "QUESTIONÁRIO",
        "ACOLHIMENTO",
        "INTEGRACAO",
        "INTEGRAÇÃO",
    },
    "DECLARACAO_RACIAL": {
        "DECLARACAO",
        "DECLARAÇÃO",
        "RACIAL",
        "RAÇA",
        "AUTODECLARACAO",
        "AUTODECLARAÇÃO",
    },
    "MBV": MBV_BLOCKED_NAME_TERMS,
}

# Estado global para evitar mutacao concorrente de tesseract_cmd em OCR paralelo
_TESSERACT_CMD_LOCK = threading.Lock()
_CONFIGURED_TESSERACT_CMD: str | None = None

# Locais comuns do Tesseract no Windows
TESSERACT_CANDIDATES = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    r"C:\Users\{}\AppData\Local\Programs\Tesseract-OCR\tesseract.exe".format(
        os.getenv("USERNAME", "")
    ),
]

# Locais comuns do Poppler no Windows
POPPLER_CANDIDATES = [
    r"C:\Users\{}\AppData\Local\Microsoft\WinGet\Packages\oschwartz10612.Poppler_Microsoft.Winget.Source_8wekyb3d8bbwe\poppler-25.07.0\Library\bin".format(
        os.getenv("USERNAME", "")
    ),
    r"C:\Program Files\poppler\Library\bin",
    r"C:\Program Files\poppler-24.08.0\Library\bin",
    r"C:\poppler\Library\bin",
    r"C:\tools\poppler\Library\bin",
    r"C:\poppler\bin",
    r"C:\tools\poppler\bin",
]

# Assinaturas para classificacao de documentos
# Nota: OCR frequentemente substitui acentos por caracteres como \ufffd
# Os padroes usam .? para aceitar qualquer caractere no lugar de acentos
DOC_TYPE_SIGNATURES = {
    "FMM": {
        "required": [
            r"[Ff]echamento\s+[Mm]ensal",
            r"[Mm]otorista",
        ],
        "optional": [
            r"[Rr]efer.?ncia",
            r"[Pp]er.?odo\s+de\s+[Rr]efer.?ncia",
            r"[Rr]eceitas?\s+[Ee]\s+[Ee]stadias",
            r"[Uu]nidade\s+[Nn]eg.?cio",
            r"[Cc]ondutor",
            r"[Mm]otor[ri]s?ta",
            r"[Ff]rete",
        ],
    },
    "CP": {
        "required": [
            r"CART.?O\s+PONTO",
        ],
        "optional": [
            r"[Jj]ornada",
            r"[Dd]ire..?o",
            r"[Ee]spera",
            r"[Nn]ome\s*:",
        ],
    },
    "FN": {
        "required": [
            # Aceita "Folha Normal", "Folha Norma", "Folha Nor'", "Folha Normza"
            r"[Ff]olha\s+[Nn]or(?:m|r|[^a-zA-Z\n]|$)",
        ],
        "optional": [
            r"[Ff]uncion.?rio",
            r"DIARIAS|HORA\s+EXTRA",
            r"MOTORISTA\s+CARRETEIRO",
            r"FROTA\s+OPERACIONAL",
        ],
    },
    "MBV": {
        "required": [
            r"MOVIMENTA.{1,3}O\s+DE\s+BENEFICI.{1,3}RIO",
        ],
        "optional": [
            r"V.?NCULO",
            r"EXCLUS.?O",
            r"[Dd]ependente",
            r"EMPREGAT.?CIO",
            r"Eu\s*,\s*[A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç\s]{4,}\s*,\s*inscrito\s+no\s+CPF",
            r"Nome\s*/\s*Cargo\s+do\s+Funcion.rio",
        ],
    },
    "AP": {
        "required": [
            r"(?:[Aa]viso\s+[Pp][Rr].?[Vv][Ii][Oo]|[Cc]ontrato\s+de\s+[Rr]escis.?o\s+[Aa]ntecipada)",
        ],
        "optional": [
            r"[Cc]ontrato\s+de\s+[Rr]escis.?o\s+[Aa]ntecipada",
            r"[Ee]mpregador",
            r"[Ii]ndenizado",
            r"[Tt]rabalhado",
            r"CTPS",
            r"[Dd]ispensa",
            r"Sr\s*[\.\(]",
        ],
    },
    "ASO_ADMISSIONAL": {
        "required": [
            r"\bASO\b",
            r"(?:[Aa]dmissiona(?:l|is)|[Ee]xame\s+[Aa]dmissiona(?:l|is)|[Ee]xame\s+de\s+[Ss]a[úu]de\s+[Oo]cupacional)",
        ],
        "optional": [
            r"[Aa]dmissiona(?:l|is)",
            r"[Ee]xame\s+[Aa]dmissiona(?:l|is)",
            r"[Pp]erfil\s+[Pp]rofissiogr[áa]fico",
        ],
    },
    "ASO_DEMISSIONAL": {
        "required": [
            r"\bASO\b",
            r"(?:[Dd]emissiona(?:l|is)|[Ee]xame\s+[Dd]emissiona(?:l|is)|[Ee]xame\s+de\s+[Dd]emiss[ãa]o)",
        ],
        "optional": [
            r"[Dd]emissiona(?:l|is)",
            r"[Ee]xame\s+[Dd]emissiona(?:l|is)",
        ],
    },
    "ATESTADO_MEDICO": {
        "required": [
            r"[Aa]testado",
        ],
        "optional": [
            r"[Mm][Ee]dic[oa]",
            r"[Mm]edico",
            r"[Mm][ée]dico",
            r"[Aa]fastamento",
        ],
    },
    "CTPS": {
        "required": [
            r"\bCTPS\b|[Cc]arteira\s+de\s+[Tt]rabalho|[Cc]arteira\s+[Pp]rofissional",
        ],
        "optional": [
            r"[Dd]igital",
            r"[Vv]erso",
            r"[Aa]nota[çc][ãa]o",
        ],
    },
    "CNH": {
        "required": [
            r"\bCNH\b|[Cc]arteira\s+[Nn]acional\s+de\s+[Hh]abilita[çc][ãa]o",
        ],
        "optional": [
            r"[Hh]abilita[çc][ãa]o",
            r"[Vv]alidade",
            r"[Cc]ondutor",
        ],
    },
    "CURRICULO": {
        "required": [
            r"[Cc]urr[íi]culo|[Cc]urriculum|[Vv]itae",
        ],
        "optional": [
            r"[Ee]xperi[êe]ncia",
            r"[Ff]orma[çc][ãa]o",
            r"[Oo]bjetivo",
        ],
    },
    "FGTS": {
        "required": [
            r"\bFGTS\b|[Ff]undo\s+de\s+[Gg]arantia|[Gg]uia\s+do\s+FGTS",
        ],
        "optional": [
            r"[Dd]igital",
            r"[Gg]uia",
            r"[Pp]agamento",
        ],
    },
    "HOLERITE": {
        "required": [
            r"[Hh]olerite|[Cc]ontracheque|[Rr]ecibo\s+de\s+[Pp]agamento",
        ],
        "optional": [
            r"[Pp]roventos",
            r"[Dd]escontos",
            r"[Ss]al[áa]rio",
        ],
    },
    "PPP": {
        "required": [
            r"\bPPP\b|[Pp]erfil\s+[Pp]rofissiogr[áa]fico|[Pp]revidenci[áa]rio",
        ],
        "optional": [
            r"[Ee]xposi[çc][ãa]o",
            r"[Rr]iscos",
            r"[Oo]cupacional",
        ],
    },
    "AVALIACAO_MOTORISTA": {
        "required": [
            r"[Aa]valiac[ãa]o",
            r"[Mm]otorist[ao]",
        ],
        "optional": [
            r"[Tt]este",
            r"[Pp]erfil",
            r"[Cc]onduta",
        ],
    },
    "TESTE_PRATICO": {
        "required": [
            r"[Tt]este",
            r"[Pp]r[áa]tic[oa]",
        ],
        "optional": [
            r"[Cc]ondutor",
            r"[Mm]otorista",
            r"[Aa]valia",
        ],
    },
    "TESTE_CONHECIMENTOS_GERAIS": {
        "required": [
            r"[Tt]este",
            r"[Cc]onheciment[oa]s?",
            r"[Gg]erais?",
        ],
        "optional": [
            r"[Qq]uestion[áa]rio",
            r"[Pp]erguntas?",
            r"[Tt]reinamento",
        ],
    },
    "TREINAMENTO_DIRECAO_DEFENSIVA": {
        "required": [
            r"[Tt]reinament[oa]",
            r"[Dd]ire[çc][ãa]o",
            r"[Dd]efensiv[oa]",
        ],
        "optional": [
            r"[Ss]eguran[çc]a",
            r"[Tt]ransito",
            r"[Mm]otorista",
        ],
    },
    "PAPELETA_CONTROLE_JORNADA": {
        "required": [
            r"[Pp]apelet[aa]",
            r"[Cc]ontrole",
            r"[Jj]ornad[aa]",
        ],
        "optional": [
            r"[Pp]onto",
            r"[Hh]or[áa]rio",
            r"[Tt]rabalho",
        ],
    },
    "QUESTIONARIO_ACOLHIMENTO": {
        "required": [
            r"[Qq]uestion[áa]ri[oa]",
            r"[Aa]colhiment[oa]",
        ],
        "optional": [
            r"[Ii]ntegrac[ãa]o",
            r"[Cc]adastro",
            r"[Pp]erfil",
        ],
    },
    "DECLARACAO_RACIAL": {
        "required": [
            r"[Dd]eclara[çc][ãa]o",
            r"[Rr]acial",
        ],
        "optional": [
            r"[Aa]utodeclara[çc][ãa]o",
            r"[Rr]a[çc][aa]",
            r"[Ee]tnic",
        ],
    },
    "NF": {
        "required": [
            r"NOTA\s+[FP]IS?CAL",
        ],
        "optional": [
            r"ELETR.?NICA",
            r"CHAVE\s+DE\s+ACESSO",
            r"DOCUMENTO\s+AUXILIAR",
            r"DANFE",
        ],
    },
    "RECIBO": {
        "required": [
            r"RECIBO",
        ],
        "optional": [
            r"RECEB[EI]",
            r"VALOR",
            r"R\$",
            r"REFER[ÊE]NCIA",
        ],
    },
    "DECLARACAO": {
        "required": [
            r"DECLARA.{0,2}[AÃ]O",
        ],
        "optional": [
            r"DECLARO",
            r"ATESTO",
            r"PARA\s+OS\s+DEVIDOS\s+FINS",
            r"CPF",
        ],
    },
    "CONTRATO": {
        "required": [
            r"CONTRATO",
        ],
        "optional": [
            r"CONTRATANTE",
            r"CONTRATADO",
            r"CL[ÁA]USULA",
            r"VIG[ÊE]NCIA",
        ],
    },
    "COMPROVANTE": {
        "required": [
            r"COMPROVANTE",
        ],
        "optional": [
            r"PAGAMENTO",
            r"TRANSFER[ÊE]NCIA",
            r"PROTOCOLO",
            r"AUTENTICA[ÇC][ÃA]O",
        ],
    },
}

DOC_TYPE_TITLE_HINTS = {
    "AVALIACAO_MOTORISTA": ["AVALIACAO MOTORISTA", "AVALIACAO DE MOTORISTA"],
    "TESTE_PRATICO": ["TESTE PRATICO"],
    "TESTE_CONHECIMENTOS_GERAIS": ["TESTE DE CONHECIMENTOS GERAIS", "TESTE CONHECIMENTOS GERAIS"],
    "TREINAMENTO_DIRECAO_DEFENSIVA": ["TREINAMENTO DIRECAO DEFENSIVA", "TREINAMENTO DE DIRECAO DEFENSIVA"],
    "PAPELETA_CONTROLE_JORNADA": ["PAPELETA CONTROLE DE JORNADA", "PAPELETA CONTROLE JORNADA"],
    "QUESTIONARIO_ACOLHIMENTO": ["QUESTIONARIO DE ACOLHIMENTO", "QUESTIONARIO ACOLHIMENTO"],
    "DECLARACAO_RACIAL": ["DECLARACAO RACIAL", "AUTODECLARACAO RACIAL"],
}

DOC_TYPE_PRIORITY = {
    "MBV": 100,
    "FMM": 95,
    "CP": 90,
    "FN": 85,
    "ASO_ADMISSIONAL": 84,
    "ASO_DEMISSIONAL": 83,
    "ATESTADO_MEDICO": 82,
    "CTPS": 81,
    "CNH": 80,
    "CURRICULO": 79,
    "FGTS": 78,
    "HOLERITE": 77,
    "PPP": 76,
    "AVALIACAO_MOTORISTA": 75,
    "TREINAMENTO_DIRECAO_DEFENSIVA": 74,
    "TESTE_CONHECIMENTOS_GERAIS": 73,
    "TESTE_PRATICO": 72,
    "PAPELETA_CONTROLE_JORNADA": 71,
    "QUESTIONARIO_ACOLHIMENTO": 70,
    "DECLARACAO_RACIAL": 69,
    "AP": 75,
    "CONTRATO": 70,
    "DECLARACAO": 65,
    "RECIBO": 60,
    "COMPROVANTE": 55,
    "NF": 50,
    "GEN": 10,
}

CUSTOM_MODELS_FILE = PROJECT_ROOT / "models" / "custom_models.json"
DEFAULT_CUSTOM_DOC_PRIORITY = 40


def _compile_signatures(signatures: dict[str, dict[str, list[str]]]) -> dict[str, dict[str, list[re.Pattern]]]:
    """Compila regex de assinaturas para classificacao eficiente."""
    compiled: dict[str, dict[str, list[re.Pattern]]] = {}
    for doc_type, sigs in signatures.items():
        required = [re.compile(p, re.IGNORECASE) for p in sigs.get("required", [])]
        optional = [re.compile(p, re.IGNORECASE) for p in sigs.get("optional", [])]
        if required:
            compiled[doc_type] = {"required": required, "optional": optional}
    return compiled


def _normalize_doc_type_name(value: str) -> str:
    """Normaliza nome de tipo de documento para chave interna."""
    normalized = re.sub(r"[^A-Za-z0-9_]+", "_", value.strip().upper())
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    return normalized


def _extend_unique_patterns(target: list[str], incoming: list[str]) -> int:
    """Acrescenta patterns novos sem duplicar os já existentes."""
    added = 0
    existing = set(target)
    for pattern in incoming:
        if pattern not in existing:
            target.append(pattern)
            existing.add(pattern)
            added += 1
    return added


def _normalize_for_ocr_match(text: str) -> str:
    """Normaliza texto para comparacoes tolerantes a OCR ruim."""
    normalized = unicodedata.normalize("NFKD", text)
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    normalized = re.sub(r"[^A-Z0-9]+", "", normalized.upper())
    return normalized


def _line_matches_phrase(ocr_line: str, phrase: str, min_ratio: float = 0.84) -> bool:
    """Compara uma linha OCR com um termo alvo mesmo quando faltam letras."""
    line_norm = _normalize_for_ocr_match(ocr_line)
    phrase_norm = _normalize_for_ocr_match(phrase)
    if not line_norm or not phrase_norm:
        return False
    if phrase_norm in line_norm:
        return True
    if len(line_norm) < max(4, len(phrase_norm) - 3):
        return False
    ratio = SequenceMatcher(None, line_norm, phrase_norm).ratio()
    return ratio >= min_ratio


def _text_has_title_hint(text: str, phrases: list[str]) -> bool:
    """Detecta titulo documental a partir de linhas OCR, com tolerancia a perdas leves."""
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()[:20]]
    lines = [line for line in lines if line]
    for phrase in phrases:
        for line in lines:
            if _line_matches_phrase(line, phrase):
                return True
    return False

# Correcoes comuns de OCR para portugues
OCR_CORRECTIONS = {
    # Correcoes existentes
    "Fecharnento": "Fechamento",
    "Motoriста": "Motorista",
    "Motrsta": "Motorista",
    "Motorisa": "Motorista",
    "Referéncia": "Referência",
    "Cart4o": "Cartão",
    "Cartao": "Cartão",
    "Beneficiário": "Beneficiário",
    "Movimentacão": "Movimentação",
    "Folha Norrnal": "Folha Normal",
    "Cartáo": "Cartão",
    # Novas correcoes baseadas nos logs de scan
    "Folha Normza": "Folha Normal",
    "Movimehtação": "Movimentação",
    "Movigientação": "Movimentação",
    "Movirentação": "Movimentação",
    "EMPREGATIÍCIO": "EMPREGATÍCIO",
    "EMPREGATICIO": "EMPREGATÍCIO",
    "BENEFICIÁARIO": "BENEFICIÁRIO",
    "BENEFICIARIO": "BENEFICIÁRIO",
    "MOVIMENTAÇO": "MOVIMENTAÇÃO",
    "TEIXEI—RA": "TEIXEIRA",
    "Folha Norm:": "Folha Normal",
    "VINCULO": "VÍNCULO",
    "AVALIACAO": "AVALIAÇÃO",
    "Avaliacao": "Avaliação",
    "QUESTIONARIO": "QUESTIONÁRIO",
    "Avaliacão": "Avaliação",
    "DECLARACAO": "DECLARAÇÃO",
    "Pratico": "Prático",
    "PRATICO": "PRÁTICO",
    "Questionario": "Questionário",
    "Declaracao": "Declaração",
    "PAPELETA": "PAPELETA",
    "Racial": "Racial",
    "Papeleta": "Papeleta",
}


COMPILED_SIGNATURES = _compile_signatures(DOC_TYPE_SIGNATURES)


def load_custom_models(custom_models_file: Path = CUSTOM_MODELS_FILE) -> dict[str, int]:
    """Carrega modelos customizados e mescla com os modelos padrao.

    Formato esperado em custom_models.json:
    {
      "doc_type_signatures": {
        "NOVO_TIPO": {"required": ["..."], "optional": ["..."]}
      },
      "ocr_corrections": {
        "texto_errado": "texto_correto"
      }
    }
    """
    stats = {
        "new_doc_types": 0,
        "updated_doc_types": 0,
        "added_required_patterns": 0,
        "added_optional_patterns": 0,
        "added_ocr_corrections": 0,
    }

    if not custom_models_file.is_file():
        return stats

    try:
        payload = json.loads(custom_models_file.read_text(encoding="utf-8"))
    except Exception:
        return stats

    custom_signatures = payload.get("doc_type_signatures", {})
    if isinstance(custom_signatures, dict):
        for raw_doc_type, signature_config in custom_signatures.items():
            if not isinstance(signature_config, dict):
                continue

            doc_type = _normalize_doc_type_name(str(raw_doc_type))
            if not doc_type:
                continue

            required = [
                str(pattern).strip()
                for pattern in signature_config.get("required", [])
                if str(pattern).strip()
            ]
            optional = [
                str(pattern).strip()
                for pattern in signature_config.get("optional", [])
                if str(pattern).strip()
            ]

            if not required:
                continue

            if doc_type not in DOC_TYPE_SIGNATURES:
                DOC_TYPE_SIGNATURES[doc_type] = {
                    "required": required,
                    "optional": optional,
                }
                DOC_TYPE_PRIORITY.setdefault(doc_type, DEFAULT_CUSTOM_DOC_PRIORITY)
                stats["new_doc_types"] += 1
                stats["added_required_patterns"] += len(required)
                stats["added_optional_patterns"] += len(optional)
            else:
                current = DOC_TYPE_SIGNATURES[doc_type]
                stats["added_required_patterns"] += _extend_unique_patterns(current["required"], required)
                stats["added_optional_patterns"] += _extend_unique_patterns(current["optional"], optional)
                stats["updated_doc_types"] += 1

    custom_corrections = payload.get("ocr_corrections", {})
    if isinstance(custom_corrections, dict):
        for wrong, correct in custom_corrections.items():
            wrong_text = str(wrong).strip()
            correct_text = str(correct).strip()
            if not wrong_text or not correct_text:
                continue
            if wrong_text not in OCR_CORRECTIONS:
                OCR_CORRECTIONS[wrong_text] = correct_text
                stats["added_ocr_corrections"] += 1

    global COMPILED_SIGNATURES
    COMPILED_SIGNATURES = _compile_signatures(DOC_TYPE_SIGNATURES)
    return stats


_ = load_custom_models()


# =============================================================================
# CONFIGURACAO DE USUARIO (config.ini)
# =============================================================================


def load_config() -> dict:
    """Carrega configuracao do config.ini. Cria arquivo padrao se nao existir."""
    config = configparser.ConfigParser()
    path_defaults = {
        "scanner_dir": str(DEFAULT_SCANNER_DIR),
    }
    monitor_defaults = {
        "watch_interval_seconds": str(DEFAULT_WATCH_INTERVAL_SECONDS),
        "file_stability_seconds": str(DEFAULT_FILE_STABILITY_SECONDS),
        "file_stability_checks": str(DEFAULT_FILE_STABILITY_CHECKS),
        "deferred_max_attempts": str(DEFAULT_DEFERRED_MAX_ATTEMPTS),
        "deferred_retry_cooldown_seconds": str(DEFAULT_DEFERRED_RETRY_COOLDOWN_SECONDS),
        "watch_max_workers": str(DEFAULT_WATCH_MAX_WORKERS),
        "metrics_log_every_cycles": str(DEFAULT_METRICS_LOG_EVERY_CYCLES),
        "quarantine_permanent_errors": "true",
        "confidence_gate_enabled": "true",
    }
    confidence_defaults = {
        "baseline": str(DEFAULT_MIN_CONFIDENCE_BASELINE),
        "fmm": "75",
        "cp": "70",
        "fn": "70",
        "mbv": "80",
        "ap": "70",
        "aso_admissional": "75",
        "aso_demissional": "75",
        "atestado_medico": "70",
        "ctps": "70",
        "cnh": "70",
        "curriculo": "70",
        "fgts": "70",
        "holerite": "70",
        "ppp": "70",
        "avaliacao_motorista": "70",
        "teste_pratico": "70",
        "teste_conhecimentos_gerais": "70",
        "treinamento_direcao_defensiva": "70",
        "papeleta_controle_jornada": "70",
        "questionario_acolhimento": "70",
        "declaracao_racial": "70",
        "nf": "70",
        "recibo": "70",
        "declaracao": "70",
        "contrato": "75",
        "comprovante": "70",
        "gen": "70",
    }

    if CONFIG_FILE.is_file():
        config.read(str(CONFIG_FILE), encoding="utf-8")

    changed = False
    if "paths" not in config:
        config["paths"] = {}
        changed = True
    if "monitor" not in config:
        config["monitor"] = {}
        changed = True
    if "confidence" not in config:
        config["confidence"] = {}
        changed = True

    for key, value in path_defaults.items():
        if key not in config["paths"]:
            config["paths"][key] = value
            changed = True

    for key, value in monitor_defaults.items():
        if key not in config["monitor"]:
            config["monitor"][key] = value
            changed = True

    for key, value in confidence_defaults.items():
        if key not in config["confidence"]:
            config["confidence"][key] = value
            changed = True

    if changed:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            f.write("# Configuracao do PDF Scanner\n")
            f.write("# Ajuste os caminhos e parametros de monitoramento conforme necessario\n\n")
            config.write(f)

    return {
        "scanner_dir": config.get("paths", "scanner_dir", fallback=str(DEFAULT_SCANNER_DIR)),
        "watch_interval_seconds": config.getint("monitor", "watch_interval_seconds", fallback=DEFAULT_WATCH_INTERVAL_SECONDS),
        "file_stability_seconds": config.getfloat("monitor", "file_stability_seconds", fallback=DEFAULT_FILE_STABILITY_SECONDS),
        "file_stability_checks": config.getint("monitor", "file_stability_checks", fallback=DEFAULT_FILE_STABILITY_CHECKS),
        "deferred_max_attempts": config.getint("monitor", "deferred_max_attempts", fallback=DEFAULT_DEFERRED_MAX_ATTEMPTS),
        "deferred_retry_cooldown_seconds": config.getint("monitor", "deferred_retry_cooldown_seconds", fallback=DEFAULT_DEFERRED_RETRY_COOLDOWN_SECONDS),
        "watch_max_workers": config.getint("monitor", "watch_max_workers", fallback=DEFAULT_WATCH_MAX_WORKERS),
        "metrics_log_every_cycles": config.getint("monitor", "metrics_log_every_cycles", fallback=DEFAULT_METRICS_LOG_EVERY_CYCLES),
        "quarantine_permanent_errors": config.getboolean("monitor", "quarantine_permanent_errors", fallback=True),
        "confidence_gate_enabled": config.getboolean("monitor", "confidence_gate_enabled", fallback=True),
        "confidence_baseline": config.getfloat("confidence", "baseline", fallback=DEFAULT_MIN_CONFIDENCE_BASELINE),
        "confidence_thresholds": {
            "FMM": config.getfloat("confidence", "fmm", fallback=75.0),
            "CP": config.getfloat("confidence", "cp", fallback=70.0),
            "FN": config.getfloat("confidence", "fn", fallback=70.0),
            "MBV": config.getfloat("confidence", "mbv", fallback=80.0),
            "AP": config.getfloat("confidence", "ap", fallback=70.0),
            "ASO_ADMISSIONAL": config.getfloat("confidence", "aso_admissional", fallback=75.0),
            "ASO_DEMISSIONAL": config.getfloat("confidence", "aso_demissional", fallback=75.0),
            "ATESTADO_MEDICO": config.getfloat("confidence", "atestado_medico", fallback=70.0),
            "CTPS": config.getfloat("confidence", "ctps", fallback=70.0),
            "CNH": config.getfloat("confidence", "cnh", fallback=70.0),
            "CURRICULO": config.getfloat("confidence", "curriculo", fallback=70.0),
            "FGTS": config.getfloat("confidence", "fgts", fallback=70.0),
            "HOLERITE": config.getfloat("confidence", "holerite", fallback=70.0),
            "PPP": config.getfloat("confidence", "ppp", fallback=70.0),
            "AVALIACAO_MOTORISTA": config.getfloat("confidence", "avaliacao_motorista", fallback=70.0),
            "TESTE_PRATICO": config.getfloat("confidence", "teste_pratico", fallback=70.0),
            "TESTE_CONHECIMENTOS_GERAIS": config.getfloat("confidence", "teste_conhecimentos_gerais", fallback=70.0),
            "TREINAMENTO_DIRECAO_DEFENSIVA": config.getfloat("confidence", "treinamento_direcao_defensiva", fallback=70.0),
            "PAPELETA_CONTROLE_JORNADA": config.getfloat("confidence", "papeleta_controle_jornada", fallback=70.0),
            "QUESTIONARIO_ACOLHIMENTO": config.getfloat("confidence", "questionario_acolhimento", fallback=70.0),
            "DECLARACAO_RACIAL": config.getfloat("confidence", "declaracao_racial", fallback=70.0),
            "NF": config.getfloat("confidence", "nf", fallback=70.0),
            "RECIBO": config.getfloat("confidence", "recibo", fallback=70.0),
            "DECLARACAO": config.getfloat("confidence", "declaracao", fallback=70.0),
            "CONTRATO": config.getfloat("confidence", "contrato", fallback=75.0),
            "COMPROVANTE": config.getfloat("confidence", "comprovante", fallback=70.0),
            "GEN": config.getfloat("confidence", "gen", fallback=70.0),
        },
    }


def list_pdf_files(scanner_dir: Path) -> list[Path]:
    """Lista PDFs na raiz da pasta, sem recursão e sem depender de caixa do sufixo."""
    return sorted(
        [p for p in scanner_dir.iterdir() if p.is_file() and p.suffix.lower() == ".pdf"],
        key=lambda item: item.name.lower(),
    )


def _is_transient_processing_error(exc: Exception) -> bool:
    """Identifica falhas transitórias comuns em monitoramento 24/7."""
    message = str(exc).lower()
    transient_markers = (
        "permission denied",
        "being used by another process",
        "used by another process",
        "sharing violation",
        "file is locked",
        "no such file",
        "cannot find the file",
        "temporarily unavailable",
    )
    return any(marker in message for marker in transient_markers)


def is_file_ready(
    pdf_path: Path,
    stability_seconds: float = DEFAULT_FILE_STABILITY_SECONDS,
    stability_checks: int = DEFAULT_FILE_STABILITY_CHECKS,
) -> bool:
    """Verifica se o arquivo parou de crescer antes de entrar no OCR."""
    try:
        previous_stat = None
        for _ in range(max(1, stability_checks)):
            current_stat = pdf_path.stat()
            if previous_stat is not None:
                if (
                    current_stat.st_size == previous_stat.st_size
                    and current_stat.st_mtime_ns == previous_stat.st_mtime_ns
                    and (time.time() - current_stat.st_mtime) >= stability_seconds
                ):
                    return True
            previous_stat = current_stat
            time.sleep(min(0.5, stability_seconds))
    except (FileNotFoundError, PermissionError, OSError):
        return False
    return False


# =============================================================================
# MODELOS
# =============================================================================


class ProcessStatus(Enum):
    RENAMED = "RENOMEADO"
    UNIDENTIFIED = "NAO IDENTIFICADO"
    SKIPPED = "IGNORADO"
    DEFERRED = "ADIADO"
    QUARANTINED = "QUARENTENADO"
    REVIEW = "REVISAO"
    ERROR = "ERRO"


@dataclass
class DocumentResult:
    original_path: Path
    status: ProcessStatus
    new_path: Path | None = None
    doc_type: str | None = None
    extracted_name: str | None = None
    extracted_period: str | None = None
    extracted_closing_number: str | None = None  # Para FMM: número de fechamento/matrícula
    confidence_score: float = 0.0
    error_message: str | None = None
    ocr_text_snippet: str = ""
    retryable_error: bool = False


@dataclass
class MonitorFileState:
    last_size: int | None = None
    last_mtime_ns: int | None = None
    stable_hits: int = 0
    defer_count: int = 0
    next_retry_at: float = 0.0
    last_status: str | None = None


# =============================================================================
# DETECCAO DE DEPENDENCIAS
# =============================================================================


def validate_tessdata():
    """Verifica que os arquivos de treinamento do Tesseract existem."""
    por_file = TESSDATA_DIR / "por.traineddata"
    if not por_file.is_file():
        raise FileNotFoundError(
            f"Arquivo por.traineddata nao encontrado em {TESSDATA_DIR}\n"
            f"Certifique-se de que a pasta tessdata/ esta ao lado de main.py"
        )


def find_tesseract_path() -> str:
    path = shutil.which("tesseract")
    if path:
        return path
    for candidate in TESSERACT_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    raise FileNotFoundError(
        "Tesseract nao encontrado. Instale de: "
        "https://github.com/UB-Mannheim/tesseract/wiki\n"
        "Ou ajuste TESSERACT_CANDIDATES no script."
    )


def configure_tesseract_command(tesseract_path: str) -> None:
    """Configura pytesseract.pytesseract.tesseract_cmd de forma thread-safe.

    Deve ser chamado antes de iniciar OCR paralelo. Mantido idempotente para
    nao reconfigurar o mesmo caminho em chamadas subsequentes.
    """
    global _CONFIGURED_TESSERACT_CMD

    if _CONFIGURED_TESSERACT_CMD == tesseract_path:
        return

    with _TESSERACT_CMD_LOCK:
        if _CONFIGURED_TESSERACT_CMD != tesseract_path:
            pytesseract.pytesseract.tesseract_cmd = tesseract_path
            _CONFIGURED_TESSERACT_CMD = tesseract_path


def find_poppler_path() -> str | None:
    path = shutil.which("pdftoppm")
    if path:
        # shutil.which returns full path to executable, but pdf2image expects directory
        return str(Path(path).parent)
    for candidate in POPPLER_CANDIDATES:
        p = Path(candidate)
        if p.is_dir() and (p / "pdftoppm.exe").is_file():
            return candidate
    # Busca generica na pasta WinGet
    winget_base = Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Packages"
    if winget_base.is_dir():
        for match in winget_base.glob("oschwartz10612.Poppler*/poppler-*/Library/bin"):
            if (match / "pdftoppm.exe").is_file():
                return str(match)
    raise FileNotFoundError(
        "Poppler nao encontrado. Baixe de:\n"
        "https://github.com/oschwartz10612/poppler-windows/releases\n"
        "Extraia e adicione a pasta 'bin' ao PATH ou ajuste POPPLER_CANDIDATES no script."
    )


def validate_environment(tesseract_path: str, poppler_path: str | None, scanner_dir: Path, logger: logging.Logger):
    """Valida que Tesseract, Poppler e permissoes estao funcionando antes de processar."""
    logger.info("Validando ambiente...")
    
    # 1. Tesseract consegue executar?
    try:
        result = subprocess.run(
            [tesseract_path, "--version"],
            capture_output=True,
            timeout=5,
            text=True
        )
        logger.info(f"  Tesseract: OK - {result.stdout.split(chr(10))[0]}")
    except Exception as e:
        raise RuntimeError(f"Tesseract não responde: {e}")
    
    # 2. Tesseract tem portugues?
    try:
        result = subprocess.run(
            [tesseract_path, "--list-langs"],
            capture_output=True,
            timeout=5,
            text=True
        )
        if "por" not in result.stdout:
            raise RuntimeError("Tesseract sem suporte a português (por.traineddata)")
        logger.info(f"  Tesseract idiomas: OK (inclui português)")
    except Exception as e:
        raise RuntimeError(f"Tesseract não consegue listar idiomas: {e}")
    
    # 3. Poppler consegue converter PDFs?
    if poppler_path:
        try:
            result = subprocess.run(
                [str(Path(poppler_path) / "pdftoppm.exe"), "-v"],
                capture_output=True,
                timeout=5,
                text=True
            )
            logger.info(f"  Poppler: OK")
        except Exception as e:
            raise RuntimeError(f"Poppler não responde: {e}")
    
    # 4. Pasta scanner tem permissoes de leitura e escrita?
    if not os.access(scanner_dir, os.R_OK):
        raise RuntimeError(f"Pasta scanner sem permissão de leitura: {scanner_dir}")
    if not os.access(scanner_dir, os.W_OK):
        raise RuntimeError(f"Pasta scanner sem permissão de escrita: {scanner_dir}")
    logger.info(f"  Pasta scanner: OK (leitura e escrita)")
    
    logger.info("Validação concluída com sucesso!")


def validate_pdf_integrity(pdf_path: Path, poppler_path: str | None, logger: logging.Logger) -> tuple[bool, bool, str | None]:
    """Valida rapidamente se o PDF pode ser aberto/convetido sem erro de sintaxe."""
    try:
        kwargs: dict[str, Any] = {
            "dpi": 72,
            "first_page": 1,
            "last_page": 1,
        }
        if poppler_path:
            kwargs["poppler_path"] = poppler_path
        preview = convert_from_path(str(pdf_path), **kwargs)
        if not preview:
            logger.warning(f"  PDF sem paginas renderizaveis: {pdf_path.name}")
            return False, False, "PDF sem paginas renderizaveis"
        return True, False, None
    except (FileNotFoundError, PermissionError, OSError) as e:
        logger.warning(f"  PDF indisponivel temporariamente: {pdf_path.name} ({e})")
        return False, True, str(e)
    except Exception as e:
        logger.warning(f"  PDF corrompido ou invalido: {pdf_path.name} ({e})")
        return False, False, str(e)


# =============================================================================
# LOGGING
# =============================================================================


def setup_logging(log_dir: Path | str | None = LOGS_DIR) -> logging.Logger:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path: Path | None = None

    if log_dir is not None:
        target_dir = Path(log_dir)
        target_dir.mkdir(exist_ok=True)
        log_path = target_dir / f"scanner_log_{timestamp}.txt"

    logger = logging.getLogger("scanner")
    logger.setLevel(logging.DEBUG)

    # Evitar handlers duplicados quando setup_logging e chamado varias vezes.
    if logger.handlers:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()

    # Arquivo - detalhado
    if log_path is not None:
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s"))
        logger.addHandler(fh)

    # Console - resumido
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(message)s"))

    logger.addHandler(ch)

    if log_path is not None:
        logger.info(f"Log salvo em: {log_path}")
    else:
        logger.info("Log em modo console-only")
    return logger


# =============================================================================
# CORRECAO DE ANO
# =============================================================================


def correct_year(raw_year: str) -> str:
    """Corrige valores de ano com erros de OCR para anos razoaveis.

    Exemplos:
        "2025" -> "2025" (valido, sem mudanca)
        "202"  -> "2026" (truncado, assume ano corrente)
        "2202" -> "2022" (troca de digitos adjacentes)
        "20"   -> "2026" (truncado)
    """
    current_year = datetime.now().year

    # Ano truncado na borda da pagina
    if len(raw_year) <= 3:
        return str(current_year)

    try:
        year_int = int(raw_year)
    except ValueError:
        return str(current_year)

    # Faixa razoavel: 2020 ate ano_corrente + 2
    min_year = 2020
    max_year = current_year + 2

    if min_year <= year_int <= max_year:
        return raw_year  # Valido, sem correcao

    # Ano fora da faixa -- tentar permutacoes de troca de digitos adjacentes
    year_str = str(year_int)
    if len(year_str) == 4:
        candidates = []
        for i in range(len(year_str) - 1):
            swapped = list(year_str)
            swapped[i], swapped[i + 1] = swapped[i + 1], swapped[i]
            try:
                candidate = int("".join(swapped))
                if min_year <= candidate <= max_year:
                    candidates.append(candidate)
            except ValueError:
                continue

        if candidates:
            # Escolher o candidato mais proximo do ano corrente
            best = min(candidates, key=lambda c: abs(c - current_year))
            return str(best)

    # Nenhuma correcao possivel -- usar ano corrente
    return str(current_year)


# =============================================================================
# PREPROCESSAMENTO DE IMAGEM
# =============================================================================


def deskew_image(gray_img: np.ndarray) -> np.ndarray:
    """Corrige rotacao de documentos escaneados."""
    coords = np.column_stack(np.where(gray_img < 128))
    if len(coords) < 100:
        return gray_img

    angle = cv2.minAreaRect(coords)[-1]

    if angle < -45:
        angle = -(90 + angle)
    else:
        angle = -angle

    if abs(angle) > 10 or abs(angle) < 0.1:
        return gray_img

    h, w = gray_img.shape
    center = (w // 2, h // 2)
    matrix = cv2.getRotationMatrix2D(center, angle, 1.0)
    rotated = cv2.warpAffine(
        gray_img, matrix, (w, h),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_REPLICATE,
    )
    return rotated


def _to_gray(pil_image: Image.Image) -> np.ndarray:
    """Converte imagem PIL para escala de cinza numpy."""
    img = np.array(pil_image)
    if len(img.shape) == 3:
        return cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    return img


def _upscale_if_small(gray: np.ndarray, min_width: int = 2000) -> np.ndarray:
    """Aumenta escala da imagem se for menor que min_width."""
    h, w = gray.shape
    if w < min_width:
        scale = min_width / w
        gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    return gray


def preprocess_image(pil_image: Image.Image) -> Image.Image:
    """Preprocessamento padrao: grayscale, upscale, deskew, threshold adaptativo, denoise."""
    gray = _to_gray(pil_image)
    gray = _upscale_if_small(gray)
    gray = deskew_image(gray)

    # Threshold adaptativo (Gaussiano)
    binary = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY, 31, 10,
    )

    # Reducao de ruido
    denoised = cv2.medianBlur(binary, 3)
    return Image.fromarray(denoised)


def preprocess_image_for_tables(pil_image: Image.Image) -> Image.Image:
    """Preprocessamento alternativo para documentos com tabelas.
    Usa threshold Otsu (melhor para tabelas) e sem median blur (preserva conteudo)."""
    gray = _to_gray(pil_image)
    gray = _upscale_if_small(gray)
    gray = deskew_image(gray)

    # Threshold Otsu - melhor para documentos tabulares
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return Image.fromarray(binary)


def preprocess_image_light(pil_image: Image.Image) -> Image.Image:
    """Preprocessamento leve para documentos manuscritos.
    Apenas grayscale, upscale e deskew. Sem binarizacao agressiva."""
    gray = _to_gray(pil_image)
    gray = _upscale_if_small(gray)
    gray = deskew_image(gray)
    return Image.fromarray(gray)


def _pil_to_gray_np(pil_image: Image.Image) -> np.ndarray:
    """Converte PIL para numpy em tons de cinza."""
    return _to_gray(pil_image)


def _apply_clahe(gray: np.ndarray) -> np.ndarray:
    """Aumenta contraste local sem binarizacao agressiva."""
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    return clahe.apply(gray)


def _extract_roi(gray_img: np.ndarray, roi: tuple[float, float, float, float]) -> np.ndarray:
    """Extrai ROI normalizado (x, y, w, h) de uma imagem em cinza."""
    h, w = gray_img.shape
    x, y, rw, rh = roi
    x1 = max(0, min(w - 1, int(x * w)))
    y1 = max(0, min(h - 1, int(y * h)))
    x2 = max(x1 + 1, min(w, int((x + rw) * w)))
    y2 = max(y1 + 1, min(h, int((y + rh) * h)))
    return gray_img[y1:y2, x1:x2]


def _resolve_mbv_template_path(page_index: int) -> Path | None:
    """Retorna caminho do template MBV de uma pagina, se existir."""
    filename = MBV_TEMPLATE_FILES.get(page_index)
    if not filename:
        return None
    candidate = MBV_TEMPLATE_DIR / filename
    return candidate if candidate.is_file() else None


def _align_to_template(page_gray: np.ndarray, template_gray: np.ndarray) -> np.ndarray:
    """Alinha pagina escaneada ao template usando ECC (afim)."""
    page_resized = page_gray
    if page_gray.shape != template_gray.shape:
        page_resized = cv2.resize(page_gray, (template_gray.shape[1], template_gray.shape[0]))

    warp = np.eye(2, 3, dtype=np.float32)
    criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 80, 1e-4)
    try:
        cv2.findTransformECC(
            template_gray.astype(np.float32),
            page_resized.astype(np.float32),
            warp,
            cv2.MOTION_AFFINE,
            criteria,
        )
        aligned = cv2.warpAffine(
            page_resized,
            warp,
            (template_gray.shape[1], template_gray.shape[0]),
            flags=cv2.INTER_LINEAR + cv2.WARP_INVERSE_MAP,
            borderMode=cv2.BORDER_REPLICATE,
        )
        return aligned
    except cv2.error:
        return page_resized


def _subtract_template_to_handwriting(page_gray: np.ndarray, template_gray: np.ndarray) -> np.ndarray:
    """Subtrai template da pagina para destacar escrita/checkmarks."""
    aligned = _align_to_template(page_gray, template_gray)
    diff = cv2.absdiff(aligned, template_gray)
    _, mask = cv2.threshold(diff, 28, 255, cv2.THRESH_BINARY)

    # Filtra ruido sem destruir traços de caneta
    kernel = np.ones((2, 2), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=1)
    highlighted = cv2.bitwise_and(aligned, aligned, mask=mask)
    return highlighted


def _checkbox_mark_ratio(gray_roi: np.ndarray) -> float:
    """Retorna razao de pixels escuros para inferir checkbox marcado."""
    if gray_roi.size == 0:
        return 0.0
    _, binary = cv2.threshold(gray_roi, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    dark_pixels = int(np.count_nonzero(binary))
    return dark_pixels / float(binary.shape[0] * binary.shape[1])


def _is_checkbox_checked(gray_roi: np.ndarray, threshold: float = 0.08) -> bool:
    """Classifica checkbox como marcado via densidade de tinta."""
    return _checkbox_mark_ratio(gray_roi) >= threshold


def _sanitize_field_text(text: str) -> str:
    """Normaliza texto OCR de um campo pequeno."""
    clean = normalize_ocr_text(text)
    clean = re.sub(r"[\r\t]+", " ", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean


def build_field_config(psm: int = 7, whitelist: str | None = None, disable_dict: bool = False) -> str:
    """Constroi configuracao OCR especifica por campo."""
    tessdata_str = str(TESSDATA_DIR).replace("\\", "/")
    parts = [
        "--oem 1",
        f"--psm {psm}",
        "-c preserve_interword_spaces=1",
        f'--tessdata-dir "{tessdata_str}"',
    ]
    if disable_dict:
        parts.append("-c load_system_dawg=0")
        parts.append("-c load_freq_dawg=0")
    if whitelist:
        parts.append(f"-c tessedit_char_whitelist={whitelist}")
    return " ".join(parts)


def _is_pytesseract_decode_error(exc: Exception) -> bool:
    """Detecta falha de decode do pytesseract em ambientes Windows/CP1252."""
    err_msg = str(exc)
    return "codec can't decode" in err_msg or "UnicodeDecodeError" in err_msg


def _strip_tessdata_dir_arg(config: str) -> str:
    """Remove --tessdata-dir da config para fallback via TESSDATA_PREFIX."""
    return config.split("--tessdata-dir")[0].strip() if "--tessdata-dir" in config else config


def ocr_image_with_confidence(
    image: Image.Image,
    tesseract_path: str,
    config: str,
) -> tuple[str, float]:
    """Executa OCR e retorna texto + confianca media (0-100)."""
    configure_tesseract_command(tesseract_path)

    try:
        data = pytesseract.image_to_data(
            image,
            lang=OCR_LANG,
            config=config,
            output_type=pytesseract.Output.DICT,
        )
    except Exception as exc:
        if not _is_pytesseract_decode_error(exc):
            raise

        # Fallback para Windows: evita decode de stderr UTF-8 em caminhos com acento.
        os.environ["TESSDATA_PREFIX"] = str(TESSDATA_DIR)
        simple_config = _strip_tessdata_dir_arg(config)
        data = pytesseract.image_to_data(
            image,
            lang=OCR_LANG,
            config=simple_config,
            output_type=pytesseract.Output.DICT,
        )

    tokens: list[str] = []
    confs: list[float] = []
    for token, conf_str in zip(data.get("text", []), data.get("conf", [])):
        token = (token or "").strip()
        if not token:
            continue
        try:
            conf_val = float(conf_str)
        except (TypeError, ValueError):
            conf_val = -1.0
        if conf_val < 0:
            continue
        tokens.append(token)
        confs.append(conf_val)

    text = " ".join(tokens).strip()
    avg_conf = sum(confs) / len(confs) if confs else 0.0
    return _sanitize_field_text(text), avg_conf


def _extract_date_from_text(text: str) -> str | None:
    """Extrai a primeira data DD/MM/AAAA e normaliza para DD-MM-AAAA."""
    match = re.search(r"(\d{2})/(\d{2})/(\d{1,4})", text)
    if not match:
        return None
    day, month, year = match.group(1), match.group(2), correct_year(match.group(3))
    return f"{day}-{month}-{year}"


def _extract_cpf_from_text(text: str) -> str | None:
    """Extrai CPF de textos com ou sem pontuacao."""
    cpf_match = re.search(r"(\d{3}[\.\-]?\d{3}[\.\-]?\d{3}[\-]?\d{2})", text)
    if not cpf_match:
        return None
    digits = re.sub(r"\D", "", cpf_match.group(1))
    if len(digits) != 11:
        return None
    return f"{digits[0:3]}.{digits[3:6]}.{digits[6:9]}-{digits[9:11]}"


def _extract_name_from_text_patterns(text: str) -> str | None:
    """Extrai nome com padroes focados em MBV/Anexo e valida conteudo."""
    patterns = [
        r"Eu\s*,\s*([A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç\s]{6,})\s*,\s*inscrito\s+no\s+CPF",
        r"Nome\s*/\s*Cargo\s+do\s+Funcion.rio\s*[:\-]?\s*([A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç\s]{6,})",
        r"Nome\s*(?:do\s+)?Titular\s*[:\-]?\s*([A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç\s]{6,})",
        r"Benefici.?rio\s*[:\-]?\s*([A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç\s]{6,})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        candidate = clean_name(match.group(1))
        if _is_valid_mbv_name(candidate):
            return candidate
    return None


def _is_plausible_person_name(name: str | None) -> bool:
    """Valida formato basico de nome de pessoa para reduzir falsos positivos."""
    if not name:
        return False

    normalized = re.sub(r"\s+", " ", name).strip().upper()
    if len(normalized) < 6 or len(normalized) > 70:
        return False

    words = [w for w in normalized.split(" ") if w]
    if len(words) < 2 or len(words) > 7:
        return False

    if re.search(r"\d", normalized):
        return False

    for word in words:
        if word in MBV_ALLOWED_SMALL_WORDS:
            continue
        only_letters = re.sub(r"[^A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ]", "", word)
        if len(only_letters) < 2:
            return False
        if len(word) > 20:
            return False

    return True


def _is_valid_name_for_doc_type(
    name: str | None,
    doc_type: str,
    confidence: float | None = None,
    min_confidence: float = 25.0,
) -> bool:
    """Valida nome extraido considerando edge cases comuns e por tipo."""
    if doc_type == "MBV":
        return _is_valid_mbv_name(name, min_confidence=min_confidence, confidence=confidence)

    if not _is_plausible_person_name(name):
        return False

    normalized = re.sub(r"\s+", " ", (name or "")).strip().upper()
    blocked_terms = set(COMMON_BLOCKED_NAME_TERMS)
    blocked_terms.update(DOC_BLOCKED_NAME_TERMS.get(doc_type, set()))

    if any(term in normalized for term in blocked_terms):
        return False

    # Protege contra linhas longas que parecem descricao/cabecalho em vez de nome.
    if len(normalized.split()) > 6:
        return False

    return True


def _is_valid_mbv_name(name: str | None, min_confidence: float = 25.0, confidence: float | None = None) -> bool:
    """Valida nome extraido de MBV para bloquear falsos positivos de formulario."""
    if not name:
        return False

    normalized = re.sub(r"\s+", " ", name).strip().upper()
    if len(normalized) < 6 or len(normalized) > 60:
        return False

    words = [w for w in normalized.split(" ") if w]
    if len(words) < 2:
        return False

    # Nomes de pessoa fisica tendem a ter 2-6 palavras; acima disso costuma ser texto do formulario.
    if len(words) > 6:
        return False

    if confidence is not None and confidence < min_confidence:
        return False

    blocked_hits = [term for term in MBV_BLOCKED_NAME_TERMS if term in normalized]
    if blocked_hits:
        return False

    # Rejeita tokens estranhos comuns de OCR ruim (simbolos longos ou com pouca letra).
    for word in words:
        if word in MBV_ALLOWED_SMALL_WORDS:
            continue
        only_letters = re.sub(r"[^A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ]", "", word)
        if len(only_letters) < 2:
            return False
        if len(word) > 20:
            return False

    if re.search(r"\d", normalized):
        return False

    return True


def extract_mbv_data_from_rois(
    pdf_path: Path,
    tesseract_path: str,
    poppler_path: str | None,
    logger: logging.Logger,
) -> dict:
    """Extrai campos MBV por ROI/template (sem depender de OCR full-page para nome/data)."""
    result: dict[str, str | None] = {
        "name": None,
        "period": None,
        "cpf": None,
    }

    name_cfg = build_field_config(psm=7, disable_dict=True)
    date_cfg = build_field_config(psm=7, whitelist="0123456789/")
    cpf_cfg = build_field_config(psm=7, whitelist="0123456789.-/")
    sparse_cfg = build_field_config(psm=11, disable_dict=True)

    try:
        images = pdf_to_images(pdf_path, poppler_path, dpi=OCR_DPI)
    except Exception as exc:
        logger.debug(f"  MBV ROI: falha convertendo PDF para imagem: {exc}")
        return result

    pages = images[:3]
    name_candidates: list[tuple[str, float, str]] = []
    date_candidates: list[str] = []

    for page_idx, pil_page in enumerate(pages):
        gray = _pil_to_gray_np(preprocess_image_light(pil_page))
        gray = _apply_clahe(gray)

        template_path = _resolve_mbv_template_path(page_idx)
        if template_path is not None:
            template_pil = Image.open(template_path)
            template_gray = _pil_to_gray_np(template_pil)
            field_layer = _subtract_template_to_handwriting(gray, template_gray)
            if field_layer.shape != gray.shape:
                field_layer = cv2.resize(field_layer, (gray.shape[1], gray.shape[0]))
        else:
            field_layer = gray
            logger.debug(f"  MBV ROI: template ausente para pagina {page_idx + 1}, usando ROI direto")

        rois = MBV_FIELD_ROIS.get(page_idx, {})

        # Checkboxes por CV (sem OCR)
        if "checkbox_area" in rois:
            checkbox_roi = _extract_roi(field_layer, rois["checkbox_area"])
            checked = _is_checkbox_checked(checkbox_roi)
            logger.debug(f"  MBV pagina {page_idx + 1}: checkbox_area checked={checked}")

        # Nome por pagina/campo conhecido
        for field_name in ("nome_titular", "nome_cargo_funcionario"):
            if field_name not in rois:
                continue

            roi_hand = _extract_roi(field_layer, rois[field_name])
            roi_raw = _extract_roi(gray, rois[field_name])
            roi_hand_pil = Image.fromarray(roi_hand)
            roi_raw_pil = Image.fromarray(roi_raw)

            text_hand, conf_hand = ocr_image_with_confidence(roi_hand_pil, tesseract_path, name_cfg)
            text_raw, conf_raw = ocr_image_with_confidence(roi_raw_pil, tesseract_path, sparse_cfg)

            best_text, best_conf = (text_hand, conf_hand) if conf_hand >= conf_raw else (text_raw, conf_raw)

            candidate = _extract_name_from_text_patterns(best_text) or clean_name(best_text)
            if _is_valid_mbv_name(candidate, confidence=best_conf):
                source = f"p{page_idx + 1}:{field_name}"
                name_candidates.append((candidate, best_conf, source))

        # CPF (preferencia pagina 1)
        if result["cpf"] is None and "cpf" in rois:
            cpf_roi = _extract_roi(field_layer, rois["cpf"])
            cpf_text, _ = ocr_image_with_confidence(Image.fromarray(cpf_roi), tesseract_path, cpf_cfg)
            result["cpf"] = _extract_cpf_from_text(cpf_text)

        # Data por pagina
        for date_field in ("data_solicitacao", "data"):
            if date_field not in rois:
                continue
            date_roi = _extract_roi(field_layer, rois[date_field])
            date_text, _ = ocr_image_with_confidence(Image.fromarray(date_roi), tesseract_path, date_cfg)
            parsed_date = _extract_date_from_text(date_text)
            if parsed_date:
                date_candidates.append(parsed_date)

    if name_candidates:
        # Prioriza maior confianca e, em empate, campos mais especificos das paginas 1/2
        name_candidates.sort(key=lambda item: (item[1], "nome_titular" in item[2], "nome_cargo_funcionario" in item[2]), reverse=True)
        result["name"] = name_candidates[0][0]
        logger.debug(f"  MBV ROI nome: {name_candidates[0][0]} ({name_candidates[0][1]:.1f}%) via {name_candidates[0][2]}")

    if date_candidates:
        result["period"] = date_candidates[0]

    return result


# =============================================================================
# OCR
# =============================================================================


def build_ocr_config(psm: int = 6) -> str:
    """Constroi string de configuracao do Tesseract.
    Usa --oem 1 (LSTM only) para melhor qualidade.
    Passa --tessdata-dir explicitamente para evitar problemas de encoding no Windows."""
    tessdata_str = str(TESSDATA_DIR).replace("\\", "/")
    return f'--oem 1 --psm {psm} --tessdata-dir "{tessdata_str}"'


def pdf_to_images(pdf_path: Path, poppler_path: str | None, dpi: int = 300) -> list:
    """Converte PDF para lista de imagens PIL. Com retry automático."""
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=5))
    def _convert():
        kwargs: dict[str, Any] = {"dpi": dpi}
        if poppler_path:
            kwargs["poppler_path"] = poppler_path
        return convert_from_path(str(pdf_path), **kwargs)
    
    return _convert()


def ocr_image(image: Image.Image, tesseract_path: str, config: str | None = None) -> str:
    """Executa OCR em uma imagem usando Tesseract. Com retry automático."""
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=5))
    def _ocr():
        cfg = config if config is not None else build_ocr_config(psm=6)
        try:
            return pytesseract.image_to_string(
                image,
                lang=OCR_LANG,
                config=cfg,
            )
        except Exception as e:
            # Tratar UnicodeDecodeError do pytesseract no Windows
            # quando caminhos contem acentos (ex: AUTOMAÇÂO)
            if _is_pytesseract_decode_error(e):
                # Tentar novamente sem --tessdata-dir (usando TESSDATA_PREFIX como fallback)
                os.environ["TESSDATA_PREFIX"] = str(TESSDATA_DIR)
                # Config simplificada sem --tessdata-dir
                simple_config = _strip_tessdata_dir_arg(cfg)
                return pytesseract.image_to_string(
                    image,
                    lang=OCR_LANG,
                    config=simple_config,
                )
            raise
    
    return _ocr()


def normalize_ocr_text(text: str) -> str:
    """Aplica correcoes conhecidas de OCR ao texto."""
    result = text
    for wrong, correct in OCR_CORRECTIONS.items():
        result = result.replace(wrong, correct)

    # Remover em-dash inseridos por OCR no meio de palavras (ex: TEIXEI—RA -> TEIXEIRA)
    result = re.sub(r'(\w)\u2014(\w)', r'\1\2', result)  # em-dash
    result = re.sub(r'(\w)\u2013(\w)', r'\1\2', result)  # en-dash

    return result


def _text_quality_ratio(text: str) -> float:
    """Calcula a proporcao de caracteres alfabeticos no texto.
    Retorna 0.0 a 1.0. Valores baixos indicam OCR de baixa qualidade (lixo)."""
    if not text.strip():
        return 0.0
    alpha_chars = sum(1 for c in text if c.isalpha())
    total_chars = sum(1 for c in text if not c.isspace())
    if total_chars == 0:
        return 0.0
    return alpha_chars / total_chars


def extract_text_from_pdf(
    pdf_path: Path, tesseract_path: str, poppler_path: str | None,
    logger: logging.Logger,
    base_dpi: int = OCR_DPI,
) -> tuple[str, str | None]:
    """Extrai texto de PDF com multi-pass OCR para melhor qualidade.

    Passo 1: OCR padrao (--psm 6, preprocessamento padrao)
    Passo 2: Se classificacao falha, tenta PSM 4 e PSM 3
    Passo 3: Se texto e lixo (>50% nao-alfa), tenta preprocessamento para tabelas
    Passo 4: Para MBV, tenta OCR de alta resolucao com preprocessamento leve
    
    Otimizacao: Converte PDF para imagens UMA VEZ e reutiliza em todos os passos.
    """
    configure_tesseract_command(tesseract_path)

    def _run_ocr_pass(processed_pages: list[Image.Image], psm: int, label: str) -> str:
        pass_text = ""
        config = build_ocr_config(psm=psm)
        for i, processed in enumerate(processed_pages):
            page_text = ocr_image(processed, tesseract_path, config=config)
            pass_text += f"\n{page_text}"
            logger.debug(f"  Pagina {i + 1} OCR ({label}): {len(page_text)} chars")
        return pass_text

    # --- Conversao unificada (apenas UMA VEZ) ---
    images = pdf_to_images(pdf_path, poppler_path, dpi=base_dpi)
    pages_to_process = images[:MAX_PAGES_TO_OCR]
    
    # Cache de preprocessamentos (reutilizados nos passos)
    preprocessed_default = [preprocess_image(img) for img in pages_to_process]
    preprocessed_tables = [preprocess_image_for_tables(img) for img in pages_to_process]

    # --- Passo 1: OCR hibrido (padrao + tabelas) com PSM 6 ---
    default_text = _run_ocr_pass(preprocessed_default, psm=6, label="padrao-psm6")
    table_text = _run_ocr_pass(preprocessed_tables, psm=6, label="tabela-psm6")

    default_normalized = normalize_ocr_text(default_text)
    table_normalized = normalize_ocr_text(table_text)

    default_type = classify_document(default_normalized)
    table_type = classify_document(table_normalized)
    default_quality = _text_quality_ratio(default_normalized)
    table_quality = _text_quality_ratio(table_normalized)

    normalized = default_normalized
    doc_type = default_type
    quality = default_quality
    selected_pages = preprocessed_default
    selected_source = "padrao"

    # Regra de selecao hibrida:
    # 1) Prioriza candidato que classifica FMM.
    # 2) Depois qualquer candidato classificado com maior confianca.
    # 3) Sem classificacao, usa melhor qualidade OCR.
    if table_type == "FMM" and default_type != "FMM":
        normalized = table_normalized
        doc_type = table_type
        quality = table_quality
        selected_pages = preprocessed_tables
        selected_source = "tabela"
    elif default_type == "FMM" and table_type != "FMM":
        pass
    elif table_type and not default_type:
        normalized = table_normalized
        doc_type = table_type
        quality = table_quality
        selected_pages = preprocessed_tables
        selected_source = "tabela"
    elif default_type and table_type:
        default_conf = get_classification_confidence(default_normalized, default_type)
        table_conf = get_classification_confidence(table_normalized, table_type)
        if table_conf > default_conf:
            normalized = table_normalized
            doc_type = table_type
            quality = table_quality
            selected_pages = preprocessed_tables
            selected_source = "tabela"
    elif table_quality > default_quality:
        normalized = table_normalized
        doc_type = table_type
        quality = table_quality
        selected_pages = preprocessed_tables
        selected_source = "tabela"

    logger.debug(
        "  Hibrido PSM6: padrao(type=%s,q=%.0f%%) vs tabela(type=%s,q=%.0f%%) -> %s",
        default_type,
        default_quality * 100,
        table_type,
        table_quality * 100,
        selected_source,
    )

    if doc_type is not None:
        quality = 1.0

    # --- Passo 2: Se nao classificou OU qualidade baixa, tentar PSMs alternativos ---
    # OTIMIZACAO v2.1: PSM 3 se mostrou mais efetivo que PSM 4 para documentos complexos
    # Testes revelaram que PSM 3 identifica 83% (5/6) vs PSM 6/4 que identificam 0%
    if doc_type is None or quality < 0.55:
        psm_order = [3, 4] if quality < 0.55 else [4, 3]  # Prioritizar PSM 3 para baixa qualidade
        for psm in psm_order:
            alt_text = _run_ocr_pass(selected_pages, psm=psm, label=f"{selected_source}-psm{psm}")
            alt_normalized = normalize_ocr_text(alt_text)
            alt_type = classify_document(alt_normalized)
            if alt_type is not None:
                logger.debug(f"  PSM {psm} (foi PSM 6: qualidade={quality:.0%})")
                normalized = alt_normalized
                doc_type = alt_type
                quality = 1.0
                break

    # --- Passo 3: Se texto e lixo, tentar preprocessamento para tabelas ---
    if quality < 0.35 and doc_type is None:
        logger.debug(f"  Qualidade baixa ({quality:.0%}) - tentando preprocessamento para tabelas")

        # Reaproveita o candidato de tabela calculado no passo hibrido.
        if table_quality > quality or table_type is not None:
            logger.debug(f"  Preprocessamento tabela melhorou: {quality:.0%} -> {table_quality:.0%}")
            normalized = table_normalized
            doc_type = table_type
            quality = table_quality

    # --- Passo 4: Para MBV, OCR de alta resolucao para campos manuscritos ---
    if doc_type == "MBV":
        logger.debug("  MBV detectado - tentando OCR alta resolucao para manuscritos")
        try:
            hi_res_dpi = max(OCR_DPI_HIRES, base_dpi)
            hi_res_images = pdf_to_images(pdf_path, poppler_path, dpi=hi_res_dpi)
            for i, img in enumerate(hi_res_images[:MAX_PAGES_TO_OCR]):
                # Preprocessamento leve (manuscrito degrada com binarizacao agressiva)
                processed = preprocess_image_light(img)
                config = build_ocr_config(psm=4)
                page_text = ocr_image(processed, tesseract_path, config=config)
                normalized += f"\n{page_text}"
                logger.debug(f"  Pagina {i + 1} OCR HiRes (psm4): {len(page_text)} chars")
        except Exception as e:
            logger.debug(f"  OCR HiRes falhou: {e}")

    return normalized, doc_type


def extract_text_from_pdf_adaptive(
    pdf_path: Path,
    tesseract_path: str,
    poppler_path: str | None,
    logger: logging.Logger,
) -> tuple[str, str | None]:
    """Executa OCR adaptativo com prioridade para PSM3 em 300 DPI e fallback para 450 DPI."""

    def _run_problematic_sequence_300() -> tuple[str, str | None]:
        images = pdf_to_images(pdf_path, poppler_path, dpi=OCR_DPI)
        pages = images[:MAX_PAGES_TO_OCR]

        attempts: list[tuple[str, list[Image.Image], int]] = [
            ("dpi300_psm3_light", [preprocess_image_light(img) for img in pages], 3),
            ("dpi300_psm3_tables", [preprocess_image_for_tables(img) for img in pages], 3),
            ("dpi300_psm6_default", [preprocess_image(img) for img in pages], 6),
        ]

        best_text = ""
        best_quality = -1.0

        for label, processed_pages, psm in attempts:
            config = build_ocr_config(psm=psm)
            text = ""
            for processed in processed_pages:
                page_text = ocr_image(processed, tesseract_path, config=config)
                text += f"\n{page_text}"

            normalized = normalize_ocr_text(text)
            doc_type = classify_document(normalized)
            quality = _text_quality_ratio(normalized)
            logger.debug(f"  Problematic seq {label}: type={doc_type}, quality={quality:.0%}")

            if quality > best_quality:
                best_quality = quality
                best_text = normalized

            if doc_type is not None:
                return normalized, doc_type

        return best_text, None

    text_300, type_300 = extract_text_from_pdf(
        pdf_path,
        tesseract_path,
        poppler_path,
        logger,
        base_dpi=OCR_DPI,
    )

    quality_300 = _text_quality_ratio(text_300)
    if type_300 is not None:
        return text_300, type_300

    if quality_300 < 0.60:
        logger.info("  Documento problematico - priorizando sequencia 300 DPI com PSM3")
        text_prob, type_prob = _run_problematic_sequence_300()
        if type_prob is not None:
            return text_prob, type_prob
        if _text_quality_ratio(text_prob) > quality_300:
            text_300 = text_prob
            quality_300 = _text_quality_ratio(text_prob)

    logger.info(f"  Two-pass fallback: sem classificacao em 300 DPI, tentando 450 DPI")
    text_450, type_450 = extract_text_from_pdf(
        pdf_path,
        tesseract_path,
        poppler_path,
        logger,
        base_dpi=OCR_DPI_HIRES,
    )

    if type_450 is not None:
        logger.info(f"  Two-pass: classificacao recuperada em 450 DPI ({type_450})")
        return text_450, type_450

    quality_450 = _text_quality_ratio(text_450)
    if quality_450 > quality_300:
        logger.debug(f"  Two-pass: mantendo texto 450 DPI por qualidade ({quality_300:.0%} -> {quality_450:.0%})")
        return text_450, None

    return text_300, None


# =============================================================================
# CLASSIFICACAO DE DOCUMENTO
# =============================================================================


def classify_document(text: str) -> str | None:
    """Classifica documento baseado em assinaturas de texto (usa regex compilado).
    Retorna o tipo com maior score ou None."""
    best_match = None
    best_score = 0
    best_priority = -1

    for doc_type, compiled_sigs in COMPILED_SIGNATURES.items():
        required_matched = all(
            pattern.search(text) for pattern in compiled_sigs['required']
        )
        if not required_matched:
            continue

        optional_score = sum(
            1 for pattern in compiled_sigs['optional']
            if pattern.search(text)
        )
        score = 10 + optional_score

        priority = DOC_TYPE_PRIORITY.get(doc_type, 0)

        if score > best_score or (score == best_score and priority > best_priority):
            best_score = score
            best_match = doc_type
            best_priority = priority

    if best_match:
        return best_match

    for doc_type, phrases in DOC_TYPE_TITLE_HINTS.items():
        if _text_has_title_hint(text, phrases):
            return doc_type

    return best_match


def get_classification_confidence(text: str, doc_type: str | None) -> float:
    """Retorna confidence score de classificacao (0 a 100)."""
    if not doc_type:
        return 0.0

    compiled = COMPILED_SIGNATURES.get(doc_type)
    if not compiled:
        return 0.0

    required = compiled["required"]
    optional = compiled["optional"]

    required_hits = sum(1 for pattern in required if pattern.search(text))
    optional_hits = sum(1 for pattern in optional if pattern.search(text))

    required_ratio = required_hits / len(required) if required else 0.0
    optional_ratio = optional_hits / len(optional) if optional else 0.0

    confidence = ((required_ratio * 0.8) + (optional_ratio * 0.2)) * 100
    return round(confidence, 1)


def get_fallback_confidence(extracted_name: str | None, extracted_period: str | None) -> float:
    """Confidence heuristica para fallback generico (GEN)."""
    if extracted_name and extracted_period:
        return 75.0
    if extracted_name:
        return 68.0
    return 0.0


def get_min_confidence_required(
    doc_type: str | None,
    confidence_thresholds: dict[str, float],
    baseline: float,
) -> float:
    if not doc_type:
        return baseline
    return confidence_thresholds.get(doc_type, baseline)


# =============================================================================
# EXTRACAO DE DADOS POR TIPO
# =============================================================================


def clean_name(raw_name: str) -> str:
    """Limpa e normaliza nome extraido por OCR."""
    name = raw_name.split("\n")[0]
    # Remover caracteres de substituicao Unicode e lixo do OCR
    name = name.replace("\ufffd", "").replace("�", "")

    # Corrigir acentos duplicados por OCR (ex: CONCEIÇAÃO -> CONCEIÇÃO)
    doubled_accents = [
        (r'ÇAÃ', 'ÇÃ'), (r'çaã', 'çã'),
        (r'ÁA', 'Á'), (r'áa', 'á'),
        (r'ÉE', 'É'), (r'ée', 'é'),
        (r'ÍI', 'Í'), (r'íi', 'í'),
        (r'ÓO', 'Ó'), (r'óo', 'ó'),
        (r'ÚU', 'Ú'), (r'úu', 'ú'),
        (r'ÃA', 'Ã'), (r'ãa', 'ã'),
        (r'ÕO', 'Õ'), (r'õo', 'õ'),
        (r'AÃ', 'Ã'), (r'aã', 'ã'),
        (r'AÁ', 'Á'), (r'aá', 'á'),
    ]
    for pattern, replacement in doubled_accents:
        name = name.replace(pattern, replacement)

    # Remover em-dash/en-dash inseridos por OCR no meio de nomes
    name = re.sub(r'(\w)\u2014(\w)', r'\1\2', name)
    name = re.sub(r'(\w)\u2013(\w)', r'\1\2', name)
    name = re.sub(r'(\w)—(\w)', r'\1\2', name)

    # Remover colchetes do OCR (ex: "TEIXE[RA" -> "TEIXERA")
    name = name.replace("[", "").replace("]", "")

    # Remover caracteres nao-alfabeticos do final
    name = re.sub(r'[^A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇa-záéíóúâêîôûãõç\s]+$', '', name)
    # Remover caracteres nao-alfabeticos do inicio (exceto letras)
    name = re.sub(r'^[^A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇa-záéíóúâêîôûãõç]+', '', name)
    # Normalizar espacos
    name = re.sub(r'\s+', ' ', name).strip()
    name = name.upper()
    # Remover sufixos muito curtos gerados por ruido OCR (ex: "DOS SANTOS AX").
    name = re.sub(r'\s+[A-Z]{1,2}$', '', name)

    if len(name) > 60:
        name = name[:60].rsplit(' ', 1)[0]
    return name


def _correct_date_in_period(date_str: str) -> str:
    """Aplica correct_year a datas no formato DD/MM/YYYY ou DD-MM-YYYY."""
    # Tentar extrair ano de diferentes formatos
    m = re.match(r'(\d{2}[/\-])(\d{2}[/\-])(\d{1,4})', date_str)
    if m:
        prefix = m.group(1) + m.group(2)
        year = correct_year(m.group(3))
        sep = "-" if "-" in date_str else "-"
        return prefix.replace("/", "-") + year
    return date_str


def _extract_named_doc_data(
    text: str,
    doc_type: str,
    name_patterns: list[str],
    date_patterns: list[str] | None = None,
    fallback_name: str | None = None,
) -> dict:
    """Extrai nome e data para documentos de cadastro simples."""
    result: dict[str, str | None] = {"name": None, "period": None}

    for pattern in name_patterns:
        match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE | re.DOTALL)
        if not match:
            continue
        candidate = clean_name(match.group(1))
        if _is_valid_name_for_doc_type(candidate, doc_type):
            result["name"] = candidate
            break

    if not result["name"] and fallback_name:
        result["name"] = fallback_name

    if date_patterns:
        for pattern in date_patterns:
            match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE | re.DOTALL)
            if not match:
                continue

            if match.lastindex and match.lastindex >= 3:
                day, month, year = match.group(1), match.group(2), correct_year(match.group(3))
                result["period"] = f"{day}-{month}-{year}"
            elif match.lastindex and match.lastindex == 2:
                month, year = match.group(1), correct_year(match.group(2))
                result["period"] = f"01-{month}-{year}" if len(month) == 2 else f"{month}-{year}"
            elif match.lastindex and match.lastindex >= 1:
                result["period"] = _normalize_competence_date(match.group(1))
            else:
                result["period"] = _normalize_competence_date(match.group(0))

            break

    return result


def extract_aso_admissional_data(text: str) -> dict:
    """Extrai dados de ASO admissional."""
    return _extract_named_doc_data(
        text,
        "ASO_ADMISSIONAL",
        name_patterns=[
            r"(?:[Nn]ome(?: do empregado)?|[Tt]rabalhador|[Ee]mpregado)\s*[:\-]?\s*([A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç\s]{6,})",
            r"\b([A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ]{2,}(?:\s+[A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ]{2,}){1,6})\b",
        ],
        date_patterns=[
            r"(?:[Dd]ata|[Ee]miss[ãa]o|[Vv]alida[çc][ãa]o)\s*[:\-]?\s*(\d{2})/(\d{2})/(\d{1,4})",
            r"(\d{2})/(\d{2})/(\d{1,4})",
        ],
        fallback_name="REVISAR NOME",
    )


def extract_aso_demissional_data(text: str) -> dict:
    """Extrai dados de ASO demissional."""
    return _extract_named_doc_data(
        text,
        "ASO_DEMISSIONAL",
        name_patterns=[
            r"(?:[Nn]ome(?: do empregado)?|[Tt]rabalhador|[Ee]mpregado)\s*[:\-]?\s*([A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç\s]{6,})",
            r"\b([A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ]{2,}(?:\s+[A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ]{2,}){1,6})\b",
        ],
        date_patterns=[
            r"(?:[Dd]ata|[Ee]miss[ãa]o|[Vv]alida[çc][ãa]o)\s*[:\-]?\s*(\d{2})/(\d{2})/(\d{1,4})",
            r"(\d{2})/(\d{2})/(\d{1,4})",
        ],
        fallback_name="REVISAR NOME",
    )


def extract_atestado_medico_data(text: str) -> dict:
    """Extrai dados de atestado medico."""
    return _extract_named_doc_data(
        text,
        "ATESTADO_MEDICO",
        name_patterns=[
            r"(?:[Nn]ome|[Pp]aciente|[Pp]aciente\s+nome)\s*[:\-]?\s*([A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç\s]{6,})",
            r"[Pp]ara\s+([A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç\s]{6,})",
        ],
        date_patterns=[
            r"(\d{2})/(\d{2})/(\d{1,4})",
        ],
        fallback_name="REVISAR NOME",
    )


def extract_ctps_data(text: str) -> dict:
    """Extrai dados de CTPS."""
    return _extract_named_doc_data(
        text,
        "CTPS",
        name_patterns=[
            r"(?:[Nn]ome|[Tt]itular)\s*[:\-]?\s*([A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç\s]{6,})",
            r"\b([A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ]{2,}(?:\s+[A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ]{2,}){1,6})\b",
        ],
        date_patterns=[
            r"(\d{2})/(\d{2})/(\d{1,4})",
        ],
        fallback_name="REVISAR NOME",
    )


def extract_cnh_data(text: str) -> dict:
    """Extrai dados de CNH."""
    return _extract_named_doc_data(
        text,
        "CNH",
        name_patterns=[
            r"(?:[Nn]ome|[Cc]ondutor|[Hh]abilitado)\s*[:\-]?\s*([A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç\s]{6,})",
            r"\b([A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ]{2,}(?:\s+[A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ]{2,}){1,6})\b",
        ],
        date_patterns=[
            r"(?:[Vv]alidade|[Ee]miss[ãa]o|[Dd]ata)\s*[:\-]?\s*(\d{2})/(\d{2})/(\d{1,4})",
            r"(\d{2})/(\d{2})/(\d{1,4})",
        ],
        fallback_name="REVISAR NOME",
    )


def extract_curriculo_data(text: str) -> dict:
    """Extrai dados de curriculum."""
    return _extract_named_doc_data(
        text,
        "CURRICULO",
        name_patterns=[
            r"(?:[Nn]ome|[Cc]andidato)\s*[:\-]?\s*([A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç\s]{6,})",
            r"^([A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ]{2,}(?:\s+[A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ]{2,}){1,6})$",
        ],
        date_patterns=[
            r"(\d{2})/(\d{2})/(\d{1,4})",
        ],
        fallback_name="REVISAR NOME",
    )


def extract_fgts_data(text: str) -> dict:
    """Extrai dados de FGTS."""
    return _extract_named_doc_data(
        text,
        "FGTS",
        name_patterns=[
            r"(?:[Nn]ome|[Tt]rabalhador|[Ee]mpregado)\s*[:\-]?\s*([A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç\s]{6,})",
            r"\b([A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ]{2,}(?:\s+[A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ]{2,}){1,6})\b",
        ],
        date_patterns=[
            r"(\d{2})/(\d{2})/(\d{1,4})",
            r"(\d{2})-(\d{2})-(\d{1,4})",
        ],
        fallback_name="REVISAR NOME",
    )


def extract_holerite_data(text: str) -> dict:
    """Extrai dados de holerite."""
    return _extract_named_doc_data(
        text,
        "HOLERITE",
        name_patterns=[
            r"(?:[Nn]ome|[Ee]mpregado|[Ff]uncion[áa]rio)\s*[:\-]?\s*([A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç\s]{6,})",
            r"\b([A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ]{2,}(?:\s+[A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ]{2,}){1,6})\b",
        ],
        date_patterns=[
            r"(?:[Cc]ompet[êe]ncia|[Rr]efer[êe]ncia|[Mm]es)\s*[:\-]?\s*(\d{2})/(\d{4})",
            r"(\d{2})/(\d{4})",
            r"(\d{2})/(\d{2})/(\d{1,4})",
        ],
        fallback_name="REVISAR NOME",
    )


def extract_ppp_data(text: str) -> dict:
    """Extrai dados de PPP."""
    return _extract_named_doc_data(
        text,
        "PPP",
        name_patterns=[
            r"(?:[Nn]ome|[Ee]mpregado|[Tt]rabalhador|[Ss]egurado)\s*[:\-]?\s*([A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç\s]{6,})",
            r"\b([A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ]{2,}(?:\s+[A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ]{2,}){1,6})\b",
        ],
        date_patterns=[
            r"(?:[Dd]ata|[Vv]alida[çc][ãa]o|[Ee]miss[ãa]o)\s*[:\-]?\s*(\d{2})/(\d{2})/(\d{1,4})",
            r"(\d{2})/(\d{2})/(\d{1,4})",
        ],
        fallback_name="REVISAR NOME",
    )


def extract_avaliacao_motorista_data(text: str) -> dict:
    """Extrai dados de avaliacao de motorista."""
    return _extract_named_doc_data(
        text,
        "AVALIACAO_MOTORISTA",
        name_patterns=[
            r"(?:[Nn]ome|[Mm]otorist[ao]|[Cc]andidato)\s*[:\-]?\s*([A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç\s]{6,})",
            r"\b([A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ]{2,}(?:\s+[A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ]{2,}){1,6})\b",
        ],
        date_patterns=[
            r"(?:[Dd]ata|[Ee]miss[ãa]o|[Aa]vali[aa][çc][ãa]o)\s*[:\-]?\s*(\d{2})/(\d{2})/(\d{1,4})",
            r"(\d{2})/(\d{2})/(\d{1,4})",
        ],
    )


def extract_teste_pratico_data(text: str) -> dict:
    """Extrai dados de teste pratico."""
    return _extract_named_doc_data(
        text,
        "TESTE_PRATICO",
        name_patterns=[
            r"(?:[Nn]ome|[Cc]andidato|[Cc]ondutor)\s*[:\-]?\s*([A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç\s]{6,})",
            r"\b([A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ]{2,}(?:\s+[A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ]{2,}){1,6})\b",
        ],
        date_patterns=[
            r"(?:[Dd]ata|[Aa]vali[aa][çc][ãa]o)\s*[:\-]?\s*(\d{2})/(\d{2})/(\d{1,4})",
            r"(\d{2})/(\d{2})/(\d{1,4})",
        ],
    )


def extract_teste_conhecimentos_gerais_data(text: str) -> dict:
    """Extrai dados de teste de conhecimentos gerais."""
    return _extract_named_doc_data(
        text,
        "TESTE_CONHECIMENTOS_GERAIS",
        name_patterns=[
            r"(?:[Nn]ome|[Cc]andidato|[Ee]mpregado)\s*[:\-]?\s*([A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç\s]{6,})",
            r"\b([A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ]{2,}(?:\s+[A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ]{2,}){1,6})\b",
        ],
        date_patterns=[
            r"(?:[Dd]ata|[Aa]plica[çc][ãa]o)\s*[:\-]?\s*(\d{2})/(\d{2})/(\d{1,4})",
            r"(\d{2})/(\d{2})/(\d{1,4})",
        ],
    )


def extract_treinamento_direcao_defensiva_data(text: str) -> dict:
    """Extrai dados de treinamento de direção defensiva."""
    return _extract_named_doc_data(
        text,
        "TREINAMENTO_DIRECAO_DEFENSIVA",
        name_patterns=[
            r"(?:[Nn]ome|[Tt]rabalhador|[Cc]ondutor|[Mm]otorist[ao])\s*[:\-]?\s*([A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç\s]{6,})",
            r"\b([A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ]{2,}(?:\s+[A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ]{2,}){1,6})\b",
        ],
        date_patterns=[
            r"(?:[Dd]ata|[Cc]ertifica[çc][ãa]o|[Ee]miss[ãa]o)\s*[:\-]?\s*(\d{2})/(\d{2})/(\d{1,4})",
            r"(\d{2})/(\d{2})/(\d{1,4})",
        ],
    )


def extract_papeleta_controle_jornada_data(text: str) -> dict:
    """Extrai dados de papeleta de controle de jornada."""
    return _extract_named_doc_data(
        text,
        "PAPELETA_CONTROLE_JORNADA",
        name_patterns=[
            r"(?:[Nn]ome|[Cc]ondutor|[Mm]otorist[ao])\s*[:\-]?\s*([A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç\s]{6,})",
            r"\b([A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ]{2,}(?:\s+[A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ]{2,}){1,6})\b",
        ],
        date_patterns=[
            r"(?:[Dd]ata|[Pp]er[íi]odo|[Cc]ompet[êe]ncia)\s*[:\-]?\s*(\d{2})/(\d{2})/(\d{1,4})",
            r"(\d{2})/(\d{2})/(\d{1,4})",
        ],
    )


def extract_questionario_acolhimento_data(text: str) -> dict:
    """Extrai dados de questionario de acolhimento."""
    return _extract_named_doc_data(
        text,
        "QUESTIONARIO_ACOLHIMENTO",
        name_patterns=[
            r"(?:[Nn]ome|[Cc]andidato|[Ee]mpregado)\s*[:\-]?\s*([A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç\s]{6,})",
            r"\b([A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ]{2,}(?:\s+[A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ]{2,}){1,6})\b",
        ],
        date_patterns=[
            r"(?:[Dd]ata|[Ee]miss[ãa]o)\s*[:\-]?\s*(\d{2})/(\d{2})/(\d{1,4})",
            r"(\d{2})/(\d{2})/(\d{1,4})",
        ],
    )


def extract_declaracao_racial_data(text: str) -> dict:
    """Extrai dados de declaracao racial."""
    return _extract_named_doc_data(
        text,
        "DECLARACAO_RACIAL",
        name_patterns=[
            r"(?:[Nn]ome|[Dd]eclarante|[Dd]eclarant[ee])\s*[:\-]?\s*([A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç\s]{6,})",
            r"\b([A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ]{2,}(?:\s+[A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ]{2,}){1,6})\b",
        ],
        date_patterns=[
            r"(?:[Dd]ata|[Ee]miss[ãa]o)\s*[:\-]?\s*(\d{2})/(\d{2})/(\d{1,4})",
            r"(\d{2})/(\d{2})/(\d{1,4})",
        ],
    )


def extract_fmm_data(text: str) -> dict:
    """Extrai dados de Fechamento Mensal por Motorista, incluindo número de fechamento."""
    result: dict[str, str | None] = {"name": None, "period": None, "closing_number": None}

    # FMM costuma trazer nome no cabecalho antes das tabelas de receitas/combustiveis.
    header_region = text[:3000]
    for marker in ("RECEITAS E ESTADIAS", "PEDÁGIO", "PEDAGIO", "COMBUST", "Data Nº Único"):
        idx = text.find(marker)
        if idx != -1:
            header_region = text[:idx]
            break

    # Nome: varias formas de "Motorista: NUMERO NOME" ou "Motorista:\nNUMERO\nNOME"
    name_patterns = [
        # Formato 1: "Motorista: 26845 RAFAEL BATISTA DA SILVA" (tudo na mesma linha)
        r'[Mm]otorista\s*[:;\.\-—–]\s*(?:\d{3,6}\s*[\-—–]?\s*)?([A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ][A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ\s]{3,})',
        # Formato 2: "Motorista:\n25355\nVANILTON VENERANDO..." (PSM 3 coloca em linhas separadas)
        r'[Mm]otorista\s*[:;\.\-—–]?\s*\n\s*\d{3,6}\s*\n\s*([A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ][A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ\s]{3,})',
        # Formato 3: "Motorista: 26845\nRAFAEL BATISTA..." (numero na mesma linha, nome na proxima)
        r'[Mm]otorista\s*[:;\.\-—–]\s*\d{3,6}\s*\n\s*([A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ][A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ\s]{3,})',
        # Formato 4: fallback para casos onde ':' vira '.' ou ';' e sem matricula clara
        r'[Mm]otorista[\s:;\.\-]+([A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][A-ZÁÀÂÃÉÊÍÓÔÕÚÇ\s]{3,})',
        # Formato 5: fallback tolerante para OCR em caixa mista
        r'[Mm]otorista[\s:;\.\-]+([A-Za-zÁÀÂÃÉÊÍÓÔÕÚÇáàâãéêíóôõúç][A-Za-zÁÀÂÃÉÊÍÓÔÕÚÇáàâãéêíóôõúç\s]{3,})',
        # Formato 4: "Condutor: NOME"
        r'[Cc]ondutor\s*[:;\.\-—–]\s*(?:\d{3,6}\s+)?([A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ][A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ\s]{3,})',
        # Formato 6: cabecalho sem label completo (caixa mista), ex: "21150 Andre Leandro ..."
        r'\b\d{3,6}\s+([A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç]{2,}(?:\s+[A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç]{2,}){2,})\b',
        # Formato 7: cabecalho sem label completo em caixa alta, ex: "21150 ANDRE LEANDRO ..."
        r'\b\d{3,6}\s+([A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ]{2,}(?:\s+[A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ]{2,}){2,})\b',
    ]
    for idx, pattern in enumerate(name_patterns):
        # Os dois ultimos padroes sao especificos para cabecalho com matricula+nome.
        is_header_fallback = idx >= len(name_patterns) - 2
        search_space = header_region if is_header_fallback else text
        name_match = re.search(pattern, search_space)
        if name_match:
            raw_name = name_match.group(1)
            raw_name = re.sub(r'\s+(?:AO|A0|CENTRE|SOMIRAL|BRASIL)\s*$', '', raw_name.strip(), flags=re.IGNORECASE)
            # Limpar nome: remover letra isolada no final (lixo de OCR, ex: "SILVA C")
            raw_name = re.sub(r'\s+[A-Z]$', '', raw_name.strip())
            cleaned = clean_name(raw_name)
            if cleaned and len(cleaned) > 3:
                upper_cleaned = cleaned.upper()
                blocked_terms = {
                    "UNIDADE", "NEGOCIO", "NEGÓCIO", "PAGINA", "PAGE", "REFERENCIA",
                    "REFERÊNCIA", "FECHAMENTO", "MOTORISTA", "DIARIAS", "DIÁRIAS", "ESTADIAS",
                    "FILIAL", "TBC", "RONDONOPOLIS", "RONDONÓPOLIS", "MT",
                }
                # Evita capturar labels/cabecalhos da tabela como se fossem nome.
                if any(term in upper_cleaned for term in blocked_terms):
                    continue
                if len(upper_cleaned.split()) < 3:
                    continue
                if is_header_fallback:
                    has_connector = any(token in f" {upper_cleaned} " for token in (" DA ", " DE ", " DOS ", " DAS "))
                    if not has_connector and len(upper_cleaned.split()) < 4:
                        continue
                if _is_valid_name_for_doc_type(cleaned, "FMM"):
                    result["name"] = cleaned
                    break

    # Extrair número de fechamento (matrícula/código do motorista)
    # Padrões: "Nº Único: 25355" ou "Matrícula: 26845" ou "N Unico:" ou variações OCR
    closing_patterns = [
        r'[Nº|N.] ?[Uu]nico[:\s]+?(\d{3,6})',  # Nº Único ou N Unico
        r'[Mm]atr[ií]?cula[:\s]+?(\d{3,6})',   # Matrícula ou Matricula
        r'[Cc]ódigo[:\s]+?(\d{3,6})',           # Código
        r'[Cc]od[.:]?[:\s]+?(\d{3,6})',         # Cod ou Code
        r'\b[Nn](?:\s|[Úú]|u)nico\b[:\s]+?(\d{3,6})',  # Fallback N unico/nico
    ]
    for pattern in closing_patterns:
        closing_match = re.search(pattern, header_region)
        if closing_match:
            result["closing_number"] = closing_match.group(1)
            break

    # Fallback final: varrer linhas iniciais procurando nome humano plausivel.
    if not result["name"]:
        header_lines = header_region.splitlines()[:220]
        for line in header_lines:
            compact = re.sub(r'\s+', ' ', line).strip()
            if not compact or any(ch.isdigit() for ch in compact):
                continue
            candidate_match = re.search(
                r'([A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç]{2,}(?:\s+[A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç]{2,}){2,})',
                compact,
            )
            if not candidate_match:
                continue

            candidate = candidate_match.group(1)
            candidate = re.sub(r'\s+(?:AO|A0|CENTRE|SOMIRAL|BRASIL)\s*$', '', candidate.strip(), flags=re.IGNORECASE)
            cleaned = clean_name(candidate)
            if not cleaned:
                continue

            upper_cleaned = cleaned.upper()
            blocked_terms = {
                "UNIDADE", "NEGOCIO", "NEGÓCIO", "PAGINA", "PAGE", "REFERENCIA", "REFERÊNCIA",
                "FECHAMENTO", "MOTORISTA", "DIARIAS", "DIÁRIAS", "ESTADIAS", "FILIAL", "TBC",
                "RONDONOPOLIS", "RONDONÓPOLIS", "POSTO", "LTDA", "RECEITA", "COMBUST", "PEDAGIO",
                "PEDÁGIO", "SEQUENCIA", "USUARIO", "IMPRESSAO", "IMPRESSÃO",
            }
            if any(term in upper_cleaned for term in blocked_terms):
                continue

            words = upper_cleaned.split()
            has_connector = any(token in f" {upper_cleaned} " for token in (" DA ", " DE ", " DOS ", " DAS "))
            if len(words) < 3:
                continue
            if not has_connector and len(words) < 4:
                continue

            if _is_valid_name_for_doc_type(cleaned, "FMM"):
                result["name"] = cleaned
                break

    # Periodo: "Fechamento: 190 21/05/2025 a 20/06/2025"
    period_pattern = r'[Ff]echamento\s*[:\-]\s*\d+\s+(\d{2}/\d{2}/\d{1,4})\s*a\s*(\d{2}/\d{2}/\d{1,4})'
    period_match = re.search(period_pattern, text)
    if period_match:
        start = _correct_date_in_period(period_match.group(1))
        end = _correct_date_in_period(period_match.group(2))
        result["period"] = f"{start} a {end}"
    else:
        # Fallback: "Referencia: 06/2025"
        ref_pattern = r'[Rr]efer.?ncia\s*[:\-]\s*(\d{2})/(\d{1,4})'
        ref_match = re.search(ref_pattern, text)
        if ref_match:
            month = ref_match.group(1)
            year = correct_year(ref_match.group(2))
            result["period"] = f"{month}-{year}"
        else:
            # Fallback adicional: "Periodo de Referencia: MM/YYYY"
            period_ref = re.search(r'[Pp]er.?odo\s+de\s+[Rr]efer.?ncia\s*[:\-]?\s*(\d{2})/(\d{1,4})', text)
            if period_ref:
                month = period_ref.group(1)
                year = correct_year(period_ref.group(2))
                result["period"] = f"{month}-{year}"

    return result


def extract_cp_data(text: str) -> dict:
    """Extrai dados de Cartao Ponto."""
    result: dict[str, str | None] = {"name": None, "period": None}

    # Nome: "Nome: RAFAEL BATISTA DA SILVA CPF: 37783364870"
    name_patterns = [
        r'[Nn]ome\s*:\s*([A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ][A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ\s]{3,?})\s+(?:CPF|$)',
        r'[Nn]ome\s*:\s*([A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ][A-Za-záéíóúâêîôûãõçÁÉÍÓÚÂÊÎÔÛÃÕÇ\s]{3,})',
    ]
    for pattern in name_patterns:
        match = re.search(pattern, text)
        if match:
            name = match.group(1).strip()
            # Remover CPF se ficou grudado
            name = re.sub(r'\s*CPF.*$', '', name)
            cleaned = clean_name(name)
            if cleaned and _is_valid_name_for_doc_type(cleaned, "CP"):
                result["name"] = cleaned
                break

    # Periodo: "Periodo de 21/07/2025 ate 20/08/2025"
    period_patterns = [
        r'[Pp]er.?odo\s+de\s+(\d{2}/\d{2}/\d{1,4})\s+at.?\s+(\d{2}/\d{2}/\d{1,4})',
        r'[Cc]ompet.?ncia\s+(\d{2})/(\d{1,4})',
    ]
    for pattern in period_patterns:
        match = re.search(pattern, text)
        if match:
            if match.lastindex == 2 and re.match(r'\d{2}/\d{2}/\d', match.group(1)):
                # Formato de range: DD/MM/YYYY a DD/MM/YYYY
                start = _correct_date_in_period(match.group(1))
                end = _correct_date_in_period(match.group(2))
                result["period"] = f"{start} a {end}"
            elif match.lastindex == 2:
                # Formato competencia: MM/YYYY
                month = match.group(1)
                year = correct_year(match.group(2))
                result["period"] = f"{month}-{year}"
            else:
                result["period"] = match.group(1).replace("/", "-")
            break

    return result


# Mapa de meses em portugues para numero
MONTH_MAP = {
    "janeiro": "01", "fevereiro": "02", "mar": "03", "marco": "03",
    "abril": "04", "maio": "05", "junho": "06",
    "julho": "07", "agosto": "08", "setembro": "09",
    "outubro": "10", "novembro": "11", "dezembro": "12",
}


def parse_month_year(text_fragment: str) -> str | None:
    """Converte 'Julho/2025' ou 'Agosto/202' para '07-2025'."""
    match = re.search(
        r'([Jj]aneiro|[Ff]evereiro|[Mm]ar(?:co|ço)?|[Aa]bril|[Mm]aio|[Jj]unho|'
        r'[Jj]ulho|[Aa]gosto|[Ss]etembro|[Oo]utubro|[Nn]ovembro|[Dd]ezembro)'
        r'\s*/\s*(\d{1,4})',
        text_fragment,
    )
    if match:
        month_name = match.group(1).lower()
        year = correct_year(match.group(2))
        month_num = MONTH_MAP.get(month_name)
        if month_num:
            return f"{month_num}-{year}"
    return None


def extract_fn_data(text: str) -> dict:
    """Extrai dados de Folha Normal."""
    result: dict[str, str | None] = {"name": None, "period": None}

    # Buscar o bloco apos "Funcionario"
    fn_block = re.search(
        r'[Ff]uncion.?rio.*?\n(.+?)(?:\n\n|\n\d{5})',
        text, re.DOTALL
    )
    if fn_block:
        block = fn_block.group(1)
        # Padrao: "530158 - SAMUEL TEIXEIRA DOS SANTOS 1010900 - FROTA..."
        # OCR pode produzir "527/20" em vez de "527720" (/ no lugar de 7)
        # Aceitar 4-8 chars de digitos e / misturados
        name_match = re.match(
            r'\s*[\d/]{4,8}\s*[-\u2013\u2014]\s*([A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ\[\]]{2,}(?:\s+[A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ\[\]]{1,})*)',
            block,
        )
        if name_match:
            name = name_match.group(1)
            cleaned = clean_name(name)
            if cleaned and _is_valid_name_for_doc_type(cleaned, "FN"):
                result["name"] = cleaned

    # Se nao encontrou pelo bloco, tentar padrao mais generico
    if not result["name"]:
        # Aceitar IDs de 4-8 digitos (OCR pode ler 7+ digitos)
        match = re.search(
            r'^\s*[\d/]{4,8}\s*[-\u2013\u2014]\s*([A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ][A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ\s\[\]]{3,?})\s+\d{5,}',
            text, re.MULTILINE,
        )
        if match:
            name = match.group(1)
            cleaned = clean_name(name)
            if cleaned and _is_valid_name_for_doc_type(cleaned, "FN"):
                result["name"] = cleaned

    # Periodo: "Julho/2025" ou "Agosto/202" (mes por extenso / ano)
    period = parse_month_year(text)
    if period:
        result["period"] = period
    else:
        # Fallback: MM/YYYY
        period_patterns = [
            r'[Cc]ompet.?ncia\s*[:\-]?\s*(\d{2})/(\d{1,4})',
            r'[Rr]efer.?ncia\s*[:\-]?\s*(\d{2})/(\d{1,4})',
        ]
        for pattern in period_patterns:
            match = re.search(pattern, text)
            if match:
                month = match.group(1)
                year = correct_year(match.group(2))
                result["period"] = f"{month}-{year}"
                break

    return result


def extract_mbv_data(text: str) -> dict:
    """Extrai dados de Movimentacao de Beneficiario."""
    result: dict[str, str | None] = {"name": None, "period": None}

    # Fallback textual para MBV (secundario). Extração principal deve ser por ROI.
    name_patterns = [
        r'[Nn]ome\s*(?:do\s+)?[Tt]itular\s*[:\-]?\s*([A-Za-záéíóúâêîôûãõçÁÉÍÓÚÂÊÎÔÛÃÕÇ][A-Za-záéíóúâêîôûãõçÁÉÍÓÚÂÊÎÔÛÃÕÇ\s]{3,})',
        r'[Bb]enefici.?rio\s*[:\-]?\s*([A-Za-záéíóúâêîôûãõçÁÉÍÓÚÂÊÎÔÛÃÕÇ][A-Za-záéíóúâêîôûãõçÁÉÍÓÚÂÊÎÔÛÃÕÇ\s]{3,})',
        r'Eu\s*,\s*([A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç\s]{6,})\s*,\s*inscrito\s+no\s+CPF',
        r'Nome\s*/\s*Cargo\s+do\s+Funcion.rio\s*[:\-]?\s*([A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç\s]{6,})',
    ]
    for pattern in name_patterns:
        match = re.search(pattern, text)
        if match:
            name = match.group(1).strip()
            name = re.sub(r'\s*CPF.*$', '', name)
            name = re.sub(r'\s*Telefone.*$', '', name)
            cleaned = clean_name(name)
            if _is_valid_mbv_name(cleaned):
                result["name"] = cleaned
                break

    # Datas: qualquer DD/MM/YYYY encontrada
    date_patterns = [
        r'[Dd]ata\s*[:\-]?\s*(\d{2})/(\d{2})/(\d{1,4})',
        r'(\d{2})/(\d{2})/(\d{1,4})',
    ]
    for pattern in date_patterns:
        match = re.search(pattern, text)
        if match:
            day, month, year = match.group(1), match.group(2), correct_year(match.group(3))
            result["period"] = f"{day}-{month}-{year}"
            break

    return result


def extract_fallback_data(text: str) -> dict:
    """Extracao generica para documentos nao classificados."""
    result: dict[str, str | None] = {"name": None, "period": None}

    name_patterns = [
        r'[Nn]ome\s*[:\-]\s*([A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç][A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç\s]{3,})',
        r'[Bb]enefici.?rio\s*[:\-]?\s*([A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç][A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç\s]{3,})',
        r'[Tt]itular\s*[:\-]?\s*([A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç][A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç\s]{3,})',
        r'Sr\s*\(?a?\)?\s*[:\-]?\s*([A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç][A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç\s]{3,})',
    ]

    for pattern in name_patterns:
        match = re.search(pattern, text)
        if match:
            candidate = re.sub(r'\s*(CPF|CTPS|RG|DATA|Telefone).*$', '', match.group(1).strip(), flags=re.IGNORECASE)
            cleaned = clean_name(candidate)
            if cleaned and _is_valid_name_for_doc_type(cleaned, "GEN"):
                result["name"] = cleaned
                break

    if not result["name"]:
        for line in text.splitlines():
            compact = re.sub(r'\s+', ' ', line).strip()
            if not compact:
                continue
            if re.fullmatch(r'[A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ\s]{8,}', compact):
                cleaned = clean_name(compact)
                if cleaned and _is_valid_name_for_doc_type(cleaned, "GEN"):
                    result["name"] = cleaned
                    break

    date_range = re.search(r'(\d{2}[/-]\d{2}[/-]\d{1,4})\s*(?:a|ate|até)\s*(\d{2}[/-]\d{2}[/-]\d{1,4})', text, re.IGNORECASE)
    if date_range:
        start = _correct_date_in_period(date_range.group(1).replace('/', '-'))
        end = _correct_date_in_period(date_range.group(2).replace('/', '-'))
        result["period"] = f"{start} a {end}"
        return result

    month_year = re.search(r'(\d{2})/(\d{1,4})', text)
    if month_year:
        month = month_year.group(1)
        year = correct_year(month_year.group(2))
        result["period"] = f"{month}-{year}"
        return result

    single_date = re.search(r'(\d{2})/(\d{2})/(\d{1,4})', text)
    if single_date:
        day, month, year = single_date.group(1), single_date.group(2), correct_year(single_date.group(3))
        result["period"] = f"{day}-{month}-{year}"

    return result


def extract_ap_data(text: str) -> dict:
    """Extrai dados de Aviso Previo."""
    result: dict[str, str | None] = {"name": None, "period": None}

    # Nome: "Sr(a): MAIKON WENDESON..." ou "A(o) Sr.(a)\nJOSIMAR DE FARIA SILVA"
    name_patterns = [
        # Formato 1: "Sr(a): NOME"
        r'Sr\s*[\.\(]\s*a?\s*\)?\s*[:\-]?\s*([A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ][A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ\s]{3,})',
        # Formato 2: "A(o) Sr.(a)\nNOME"
        r'[Aa]\s*\(o\)\s+Sr\.?\s*\(a\)\s*\n\s*([A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ][A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ\s]{3,})',
    ]
    for pattern in name_patterns:
        match = re.search(pattern, text)
        if match:
            name = match.group(1).strip()
            name = re.sub(r'\s*(CPF|CTPS|Presente|presente).*$', '', name)
            cleaned = clean_name(name)
            if cleaned and _is_valid_name_for_doc_type(cleaned, "AP"):
                result["name"] = cleaned
                break

    # Data: DD/MM/YYYY no documento
    date_patterns = [
        r'(\d{2})/(\d{2})/(\d{1,4})',
    ]
    for pattern in date_patterns:
        match = re.search(pattern, text)
        if match:
            day, month, year = match.group(1), match.group(2), correct_year(match.group(3))
            result["period"] = f"{day}-{month}-{year}"
            break

    return result


def extract_nf_data(text: str) -> dict:
    """Extrai dados de Nota Fiscal com fallback para emitente/destinatário."""
    result: dict[str, str | None] = {"name": None, "period": None}

    name_patterns = [
        r'(?:Raz.?o Social|Emitente|Fornecedor|Destinat.?rio|Nome do Destinat.?rio|Tomador)\s*[:\-]?\s*([A-Za-z0-9ÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç&\./\-,\s]{4,})',
        r'(?:Empresa|Cliente)\s*[:\-]?\s*([A-Za-z0-9ÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç&\./\-,\s]{4,})',
    ]
    for pattern in name_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        candidate = clean_name(match.group(1))
        if candidate:
            result["name"] = candidate
            break

    date_patterns = [
        r'(?:Data de Emiss.?o|Emiss.?o|Data)\s*[:\-]?\s*(\d{2}/\d{2}/\d{1,4})',
        r'(\d{2}/\d{2}/\d{1,4})',
    ]
    for pattern in date_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        result["period"] = _correct_date_in_period(match.group(1).replace("/", "-"))
        break

    if not result["name"]:
        result["name"] = "NOTA FISCAL"

    return result


def extract_recibo_data(text: str) -> dict:
    """Extrai dados de Recibo com heuristica administrativa."""
    result = extract_fallback_data(text)
    if not result.get("name"):
        result["name"] = "RECIBO"
    return result


def extract_declaracao_data(text: str) -> dict:
    """Extrai dados de Declaracao com heuristica administrativa."""
    result = extract_fallback_data(text)
    if not result.get("name"):
        result["name"] = "DECLARACAO"
    return result


def extract_contrato_data(text: str) -> dict:
    """Extrai dados de Contrato com heuristica administrativa."""
    result = extract_fallback_data(text)
    if not result.get("name"):
        result["name"] = "CONTRATO"
    return result


def extract_comprovante_data(text: str) -> dict:
    """Extrai dados de Comprovante com heuristica administrativa."""
    result = extract_fallback_data(text)
    if not result.get("name"):
        result["name"] = "COMPROVANTE"
    return result


EXTRACTORS = {
    "FMM": extract_fmm_data,
    "CP": extract_cp_data,
    "FN": extract_fn_data,
    "MBV": extract_mbv_data,
    "AP": extract_ap_data,
    "ASO_ADMISSIONAL": extract_aso_admissional_data,
    "ASO_DEMISSIONAL": extract_aso_demissional_data,
    "ATESTADO_MEDICO": extract_atestado_medico_data,
    "CTPS": extract_ctps_data,
    "CNH": extract_cnh_data,
    "CURRICULO": extract_curriculo_data,
    "FGTS": extract_fgts_data,
    "HOLERITE": extract_holerite_data,
    "PPP": extract_ppp_data,
    "AVALIACAO_MOTORISTA": extract_avaliacao_motorista_data,
    "TESTE_PRATICO": extract_teste_pratico_data,
    "TESTE_CONHECIMENTOS_GERAIS": extract_teste_conhecimentos_gerais_data,
    "TREINAMENTO_DIRECAO_DEFENSIVA": extract_treinamento_direcao_defensiva_data,
    "PAPELETA_CONTROLE_JORNADA": extract_papeleta_controle_jornada_data,
    "QUESTIONARIO_ACOLHIMENTO": extract_questionario_acolhimento_data,
    "DECLARACAO_RACIAL": extract_declaracao_racial_data,
    "NF": extract_nf_data,
    "RECIBO": extract_recibo_data,
    "DECLARACAO": extract_declaracao_data,
    "CONTRATO": extract_contrato_data,
    "COMPROVANTE": extract_comprovante_data,
}


def extract_document_data(text: str, doc_type: str) -> dict:
    """Despacha extracao de dados para o extrator correto."""
    extractor = EXTRACTORS.get(doc_type)
    if extractor is None:
        return {"name": None, "period": None}
    return extractor(text)


# =============================================================================
# RENOMEACAO DE ARQUIVOS
# =============================================================================


def sanitize_filename(name: str) -> str:
    """Remove caracteres invalidos e normaliza nome de arquivo."""
    sanitized = re.sub(r'[\\/:*?"<>|]', '', name)
    sanitized = re.sub(r'\s+', ' ', sanitized).strip()
    if len(sanitized) > 200:
        sanitized = sanitized[:200].rsplit(' ', 1)[0]
    return sanitized


DOC_TYPE_LABELS = {
    "FMM": "FECHAMENTO MENSAL MOTORISTA",
    "CP": "CARTAO PONTO",
    "FN": "FOLHA NORMAL",
    "MBV": "MOVIMENTACAO BENEFICIARIO",
    "AP": "AVISO PREVIO",
    "ASO_ADMISSIONAL": "ASO ADMISSIONAL",
    "ASO_DEMISSIONAL": "ASO DEMISSIONAL",
    "ATESTADO_MEDICO": "ATESTADO MEDICO",
    "CTPS": "CTPS",
    "CNH": "CNH",
    "CURRICULO": "CURRICULO",
    "FGTS": "FGTS",
    "HOLERITE": "HOLERITE",
    "PPP": "PPP",
    "AVALIACAO_MOTORISTA": "AVALIACAO MOTORISTA",
    "TESTE_PRATICO": "TESTE PRATICO",
    "TESTE_CONHECIMENTOS_GERAIS": "TESTE CONHECIMENTOS GERAIS",
    "TREINAMENTO_DIRECAO_DEFENSIVA": "TREINAMENTO DIRECAO DEFENSIVA",
    "PAPELETA_CONTROLE_JORNADA": "PAPELETA CONTROLE JORNADA",
    "QUESTIONARIO_ACOLHIMENTO": "QUESTIONARIO ACOLHIMENTO",
    "DECLARACAO_RACIAL": "DECLARACAO RACIAL",
    "NF": "NOTA FISCAL",
    "RECIBO": "RECIBO",
    "DECLARACAO": "DECLARACAO",
    "CONTRATO": "CONTRATO",
    "COMPROVANTE": "COMPROVANTE",
    "GEN": "DOCUMENTO",
}


def _doc_type_to_label(doc_type: str) -> str:
    """Converte tipo interno para rotulo legivel no nome do arquivo."""
    if not doc_type:
        return "DOCUMENTO"

    normalized = re.sub(r"[^A-Za-z0-9_]+", "_", doc_type.strip().upper())
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    if not normalized:
        return "DOCUMENTO"

    if normalized in DOC_TYPE_LABELS:
        return DOC_TYPE_LABELS[normalized]

    # Para tipos customizados (ex.: ASO_ADMISSIONAL -> ASO ADMISSIONAL)
    return normalized.replace("_", " ")


def _normalize_competence_date(period: str | None) -> str:
    """Normaliza competencia para uma data unica no formato DD-MM-YYYY.

    Regras:
    - Se houver data completa no texto, usa a primeira encontrada.
    - Se houver apenas MM-YYYY, converte para 01-MM-YYYY.
    - Se ausente, retorna SEM DATA.
    """
    if not period:
        return "SEM DATA"

    text = period.strip()
    if not text:
        return "SEM DATA"

    full_date = re.search(r"(\d{2})[/-](\d{2})[/-](\d{1,4})", text)
    if full_date:
        day, month, year = full_date.group(1), full_date.group(2), correct_year(full_date.group(3))
        return f"{day}-{month}-{year}"

    month_year = re.search(r"(\d{2})[/-](\d{4})", text)
    if month_year:
        month, year = month_year.group(1), month_year.group(2)
        return f"01-{month}-{year}"

    return "SEM DATA"


def build_new_filename(doc_type: str, name: str, period: str | None, closing_number: str | None = None) -> str:
    """Constroi nome no padrão global: ARQUIVO - EMPREGADO - DATA.pdf."""
    _ = closing_number  # Mantido para compatibilidade de chamada
    doc_label = _doc_type_to_label(doc_type)
    competence_date = _normalize_competence_date(period)
    raw = f"{doc_label} - {name} - {competence_date}.pdf"
    return sanitize_filename(raw)


def resolve_filename_conflict(target_path: Path) -> Path:
    """Resolve conflitos de nome adicionando (1), (2), etc."""
    if not target_path.exists():
        return target_path

    stem = target_path.stem
    suffix = target_path.suffix
    parent = target_path.parent
    counter = 1

    while True:
        new_name = f"{stem} ({counter}){suffix}"
        new_path = parent / new_name
        if not new_path.exists():
            return new_path
        counter += 1
        if counter > 100:
            raise RuntimeError(f"Muitos duplicados para: {stem}")


def quarantine_failed_pdf(pdf_path: Path, scanner_dir: Path, logger: logging.Logger) -> Path | None:
    """Move um PDF com erro permanente para uma pasta de quarentena."""
    quarantine_dir = scanner_dir / QUARANTINE_DIR_NAME
    quarantine_dir.mkdir(exist_ok=True)

    target_path = resolve_filename_conflict(quarantine_dir / pdf_path.name)
    try:
        shutil.move(str(pdf_path), str(target_path))
        logger.warning(f"  PDF enviado para quarentena: {target_path.name}")
        return target_path
    except Exception as e:
        logger.warning(f"  Falha ao mover para quarentena: {e}")
        return None


def move_to_review_queue(pdf_path: Path, scanner_dir: Path, logger: logging.Logger) -> Path | None:
    """Move um PDF para fila de revisao manual."""
    review_dir = scanner_dir / REVIEW_DIR_NAME
    review_dir.mkdir(exist_ok=True)

    target_path = resolve_filename_conflict(review_dir / pdf_path.name)
    try:
        shutil.move(str(pdf_path), str(target_path))
        logger.warning(f"  PDF enviado para revisao: {target_path.name}")
        return target_path
    except Exception as e:
        logger.warning(f"  Falha ao mover para revisao: {e}")
        return None


# =============================================================================
# CHECKPOINT E RECOVERY
# =============================================================================


def build_checkpoint_key(pdf_path: Path) -> str:
    """Gera chave de checkpoint baseada em nome, tamanho e data de modificacao."""
    stat = pdf_path.stat()
    return f"{pdf_path.name}|{stat.st_size}|{stat.st_mtime_ns}"

def save_checkpoint(processed_files: set, checkpoint_file: Path):
    """Salva lista de arquivos ja processados para recovery."""
    import json
    checkpoint_file.parent.mkdir(parents=True, exist_ok=True)
    tmp_file = checkpoint_file.with_name(f"{checkpoint_file.name}.tmp")
    tmp_file.write_text(
        json.dumps(sorted(list(processed_files)), indent=2),
        encoding='utf-8'
    )
    tmp_file.replace(checkpoint_file)


def load_checkpoint(checkpoint_file: Path) -> set:
    """Carrega lista de arquivos ja processados."""
    import json
    if checkpoint_file.exists():
        try:
            data = json.loads(checkpoint_file.read_text(encoding='utf-8'))
            return set(data)
        except Exception:
            return set()
    return set()


def clear_checkpoint(checkpoint_file: Path):
    """Limpa checkpoint ao final bem-sucedido."""
    if checkpoint_file.exists():
        checkpoint_file.unlink()


def collect_pending_pdf_files(scanner_dir: Path, processed_files: set[str]) -> list[Path]:
    """Lista PDFs ainda nao processados, ignorando arquivos que desapareceram no meio da varredura."""
    pending_files: list[Path] = []
    for pdf_path in list_pdf_files(scanner_dir):
        try:
            checkpoint_key = build_checkpoint_key(pdf_path)
        except FileNotFoundError:
            continue

        if checkpoint_key not in processed_files:
            pending_files.append(pdf_path)

    return pending_files


# =============================================================================


def process_single_pdf(
    pdf_path: Path,
    tesseract_path: str,
    poppler_path: str | None,
    scanner_dir: Path,
    logger: logging.Logger,
    defer_on_transient: bool = False,
    confidence_gate_enabled: bool = False,
    confidence_thresholds: dict[str, float] | None = None,
    confidence_baseline: float = DEFAULT_MIN_CONFIDENCE_BASELINE,
) -> DocumentResult:
    """Processa um unico PDF: OCR -> classificacao -> extracao -> renomeacao."""
    result = DocumentResult(original_path=pdf_path, status=ProcessStatus.ERROR)
    thresholds = confidence_thresholds or {}

    try:
        # Validacao de integridade antes de OCR
        is_valid, is_transient, integrity_error = validate_pdf_integrity(pdf_path, poppler_path, logger)
        if not is_valid:
            if defer_on_transient and is_transient:
                result.status = ProcessStatus.DEFERRED
                result.error_message = "Arquivo ainda nao esta pronto para processamento"
                result.retryable_error = True
            else:
                result.status = ProcessStatus.ERROR
                result.error_message = integrity_error or "PDF corrompido ou ilegivel"
            return result

        # OCR adaptativo (two-pass 300 -> 450)
        full_text, doc_type = extract_text_from_pdf_adaptive(pdf_path, tesseract_path, poppler_path, logger)
        result.ocr_text_snippet = full_text[:300]
        logger.debug(f"  OCR preview: {full_text[:200]!r}")

        # Classificar
        result.doc_type = doc_type
        result.confidence_score = get_classification_confidence(full_text, doc_type)
        logger.info(f"  Confidence: {result.confidence_score:.1f}%")

        if doc_type is None:
            logger.warning("  Tipo nao identificado - tentando fallback extraction generico")
            fallback = extract_fallback_data(full_text)
            result.extracted_name = fallback.get("name")
            result.extracted_period = fallback.get("period")

            if not result.extracted_name:
                logger.warning(f"  Fallback sem nome - arquivo mantido sem alteracao")
                result.status = ProcessStatus.UNIDENTIFIED
                return result

            result.doc_type = "GEN"
            result.confidence_score = get_fallback_confidence(result.extracted_name, result.extracted_period)

            logger.info(f"  Fallback nome: {result.extracted_name}")
            if result.extracted_period:
                logger.info(f"  Fallback periodo: {result.extracted_period}")
            else:
                logger.info("  Fallback periodo: ausente")
        else:
            logger.info(f"  Tipo: {doc_type}")

            # MBV: usar extração por ROI/template para evitar falso positivo no texto full-page.
            if doc_type == "MBV":
                data = extract_mbv_data_from_rois(pdf_path, tesseract_path, poppler_path, logger)
                result.extracted_name = data.get("name")
                result.extracted_period = data.get("period")

                if not result.extracted_name:
                    # Não usa fallback caixa alta para MBV; força revisão humana.
                    result.extracted_name = "REVISAR NOME"

                if not result.extracted_period:
                    result.extracted_period = "SEM PERIODO"
            else:
                # Extrair dados para demais tipos
                data = extract_document_data(full_text, doc_type)
                result.extracted_name = data.get("name")
                result.extracted_period = data.get("period")
                # Para FMM, também extrair número de fechamento (matrícula/código)
                if doc_type == "FMM":
                    result.extracted_closing_number = data.get("closing_number")

            # Validação final transversal de nome (edge cases para todos os tipos).
            if result.extracted_name and result.extracted_name != "REVISAR NOME":
                validation_type = result.doc_type if result.doc_type else "GEN"
                if not _is_valid_name_for_doc_type(result.extracted_name, validation_type):
                    logger.warning(
                        f"  Nome rejeitado por validacao ({validation_type}): {result.extracted_name}"
                    )
                    if validation_type == "MBV":
                        result.extracted_name = "REVISAR NOME"
                    else:
                        result.status = ProcessStatus.UNIDENTIFIED
                        return result

            if not result.extracted_name:
                logger.warning(f"  Tipo={doc_type} mas nome nao encontrado - arquivo mantido sem alteracao")
                result.status = ProcessStatus.UNIDENTIFIED
                return result

            if not result.extracted_period and doc_type not in {"MBV"}:
                logger.warning(f"  Tipo={doc_type} nome={result.extracted_name} mas periodo nao encontrado")
                result.extracted_period = "SEM PERIODO"

            logger.info(f"  Nome: {result.extracted_name}")
            if result.extracted_closing_number and result.doc_type == "FMM":
                logger.info(f"  Numero de Fechamento: {result.extracted_closing_number}")
            if result.extracted_period:
                logger.info(f"  Data: {result.extracted_period}")
            else:
                logger.info("  Data: ausente")

        min_required = get_min_confidence_required(result.doc_type, thresholds, confidence_baseline)
        if confidence_gate_enabled and result.confidence_score < min_required:
            logger.warning(
                f"  Confidence abaixo do minimo para {result.doc_type} "
                f"({result.confidence_score:.1f}% < {min_required:.1f}%)."
            )
            review_path = move_to_review_queue(pdf_path, scanner_dir, logger)
            if review_path is None:
                result.status = ProcessStatus.ERROR
                result.error_message = "Falha ao enviar para revisao"
            else:
                result.status = ProcessStatus.REVIEW
                result.new_path = review_path
            return result

        # Renomear (no diretorio do scanner, nao hardcoded)
        type_for_filename = result.doc_type if result.doc_type else "GEN"
        new_name = build_new_filename(
            type_for_filename, result.extracted_name, result.extracted_period,
            closing_number=result.extracted_closing_number
        )
        target_path = scanner_dir / new_name
        target_path = resolve_filename_conflict(target_path)

        pdf_path.rename(target_path)
        result.new_path = target_path
        result.status = ProcessStatus.RENAMED
        logger.info(f"  RENOMEADO -> {target_path.name}")

    except Exception as e:
        if defer_on_transient and _is_transient_processing_error(e):
            result.error_message = str(e)
            result.status = ProcessStatus.DEFERRED
            result.retryable_error = True
            logger.warning(f"  Arquivo adiado temporariamente: {e}")
        else:
            result.error_message = str(e)
            result.status = ProcessStatus.ERROR
            logger.error(f"  ERRO: {e}", exc_info=True)

    return result


def process_pdf_batch(
    pdf_files: list[Path],
    tesseract_path: str,
    poppler_path: str | None,
    scanner_dir: Path,
    logger: logging.Logger,
    processed_files: set[str],
    checkpoint_file: Path,
    defer_on_transient: bool = False,
    quarantine_permanent_errors: bool = False,
    max_workers: int | None = None,
    confidence_gate_enabled: bool = False,
    confidence_thresholds: dict[str, float] | None = None,
    confidence_baseline: float = DEFAULT_MIN_CONFIDENCE_BASELINE,
) -> list[DocumentResult]:
    """Processa uma lista de PDFs com paralelismo e atualiza o checkpoint."""
    results: list[DocumentResult] = []

    if not pdf_files:
        return results

    configured_workers = DEFAULT_WATCH_MAX_WORKERS if max_workers is None else max_workers
    if configured_workers <= 0:
        configured_workers = DEFAULT_WATCH_MAX_WORKERS
    num_workers = min(configured_workers, len(pdf_files))

    with ThreadPoolExecutor(max_workers=num_workers) as executor:
        futures = {
            executor.submit(
                process_single_pdf,
                pdf_path,
                tesseract_path,
                poppler_path,
                scanner_dir,
                logger,
                defer_on_transient,
                confidence_gate_enabled,
                confidence_thresholds,
                confidence_baseline,
            ): (pdf_path, idx)
            for idx, pdf_path in enumerate(pdf_files, 1)
        }

        for future in as_completed(futures):
            pdf_path, idx = futures[future]
            try:
                result = future.result()
                results.append(result)
                try:
                    checkpoint_key = build_checkpoint_key(pdf_path)
                except FileNotFoundError:
                    checkpoint_key = None

                if result.status == ProcessStatus.RENAMED:
                    logger.info(f"[{idx}/{len(pdf_files)}] {pdf_path.name} -> RENOMEADO")
                    if checkpoint_key is not None:
                        processed_files.add(checkpoint_key)
                elif result.status == ProcessStatus.UNIDENTIFIED:
                    logger.info(f"[{idx}/{len(pdf_files)}] {pdf_path.name} -> NAO IDENTIFICADO")
                    if checkpoint_key is not None:
                        processed_files.add(checkpoint_key)
                elif result.status == ProcessStatus.SKIPPED:
                    logger.info(f"[{idx}/{len(pdf_files)}] {pdf_path.name} -> IGNORADO")
                    if checkpoint_key is not None:
                        processed_files.add(checkpoint_key)
                elif result.status == ProcessStatus.DEFERRED:
                    logger.info(f"[{idx}/{len(pdf_files)}] {pdf_path.name} -> ADIADO")
                elif result.status == ProcessStatus.REVIEW:
                    logger.info(f"[{idx}/{len(pdf_files)}] {pdf_path.name} -> REVISAO")
                    if checkpoint_key is not None:
                        processed_files.add(checkpoint_key)
                elif result.status == ProcessStatus.ERROR and defer_on_transient and not result.retryable_error:
                    quarantined_path = None
                    if quarantine_permanent_errors:
                        quarantined_path = quarantine_failed_pdf(pdf_path, scanner_dir, logger)
                        if quarantined_path is not None:
                            result.status = ProcessStatus.QUARANTINED
                            result.new_path = quarantined_path

                    if result.status == ProcessStatus.QUARANTINED:
                        logger.info(f"[{idx}/{len(pdf_files)}] {pdf_path.name} -> QUARENTENADO")
                    else:
                        logger.info(f"[{idx}/{len(pdf_files)}] {pdf_path.name} -> ERRO DEFINITIVO")

                    if checkpoint_key is not None:
                        processed_files.add(checkpoint_key)
                else:
                    logger.error(f"[{idx}/{len(pdf_files)}] {pdf_path.name} -> ERRO")

                save_checkpoint(processed_files, checkpoint_file)
            except Exception as e:
                logger.error(f"[{idx}/{len(pdf_files)}] {pdf_path.name} - Erro na thread: {e}", exc_info=True)

    return results


def log_batch_summary(logger: logging.Logger, results: list[DocumentResult]) -> None:
    """Loga resumo de um lote processado."""
    logger.info("\n" + "=" * 60)
    logger.info("  RESUMO")
    logger.info("=" * 60)
    renamed = sum(1 for r in results if r.status == ProcessStatus.RENAMED)
    unid = sum(1 for r in results if r.status == ProcessStatus.UNIDENTIFIED)
    skipped = sum(1 for r in results if r.status == ProcessStatus.SKIPPED)
    deferred = sum(1 for r in results if r.status == ProcessStatus.DEFERRED)
    quarantined = sum(1 for r in results if r.status == ProcessStatus.QUARANTINED)
    review = sum(1 for r in results if r.status == ProcessStatus.REVIEW)
    errors = sum(1 for r in results if r.status == ProcessStatus.ERROR)
    logger.info(f"  Renomeados:        {renamed}")
    logger.info(f"  Nao identificados: {unid}")
    logger.info(f"  Ignorados (nao-RH): {skipped}")
    logger.info(f"  Adiados:           {deferred}")
    logger.info(f"  Quarentenados:     {quarantined}")
    logger.info(f"  Em revisao:        {review}")
    logger.info(f"  Erros:             {errors}")
    logger.info(f"  Total processados: {len(results)}")
    logger.info("=" * 60)


def _status_counts(results: list[DocumentResult]) -> dict[ProcessStatus, int]:
    """Conta ocorrencias por status em um lote de resultados."""
    return {status: sum(1 for item in results if item.status == status) for status in ProcessStatus}


def _percent(part: int, total: int) -> float:
    """Calcula percentual com protecao para divisor zero."""
    if total <= 0:
        return 0.0
    return (part / total) * 100.0


def log_monitor_cycle_metrics(
    logger: logging.Logger,
    cycle_number: int,
    cycle_elapsed_seconds: float,
    processed_in_cycle: int,
    deferred_in_cycle: int,
    review_in_cycle: int,
    totals: dict[str, float],
) -> None:
    """Loga metricas de ciclo e acumuladas para tuning continuo do monitor."""
    avg_cycle_seconds = cycle_elapsed_seconds / processed_in_cycle if processed_in_cycle > 0 else 0.0
    deferred_rate_cycle = _percent(deferred_in_cycle, processed_in_cycle)
    review_rate_cycle = _percent(review_in_cycle, processed_in_cycle)

    total_processed = int(totals["processed"])
    total_deferred = int(totals["deferred"])
    total_review = int(totals["review"])
    total_cycles = int(totals["cycles"])
    total_elapsed = totals["elapsed_seconds"]
    avg_total_seconds = total_elapsed / total_processed if total_processed > 0 else 0.0
    deferred_rate_total = _percent(total_deferred, total_processed)
    review_rate_total = _percent(total_review, total_processed)

    logger.info(
        "Metricas ciclo #%s | tempo_total=%.2fs | tempo_medio=%.2fs/arquivo | "
        "taxa_adiados=%.1f%% | taxa_revisao=%.1f%% | processados=%s",
        cycle_number,
        cycle_elapsed_seconds,
        avg_cycle_seconds,
        deferred_rate_cycle,
        review_rate_cycle,
        processed_in_cycle,
    )
    logger.info(
        "Metricas acumuladas (%s ciclos) | tempo_medio=%.2fs/arquivo | "
        "taxa_adiados=%.1f%% | taxa_revisao=%.1f%% | processados=%s",
        total_cycles,
        avg_total_seconds,
        deferred_rate_total,
        review_rate_total,
        total_processed,
    )


def write_monitor_heartbeat(
    heartbeat_file: Path,
    cycle_number: int,
    watch_interval_seconds: int,
    pending_files: int,
    processed_in_cycle: int,
    waiting_copy: int,
    waiting_cooldown: int,
) -> None:
    """Atualiza heartbeat do monitor para deteccao de travamento silencioso."""
    payload = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "updated_at_epoch": time.time(),
        "cycle": cycle_number,
        "watch_interval_seconds": watch_interval_seconds,
        "pending_files": pending_files,
        "processed_in_cycle": processed_in_cycle,
        "waiting_copy": waiting_copy,
        "waiting_cooldown": waiting_cooldown,
    }
    heartbeat_file.parent.mkdir(parents=True, exist_ok=True)
    tmp_file = heartbeat_file.with_name(f"{heartbeat_file.name}.tmp")
    tmp_file.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
    tmp_file.replace(heartbeat_file)


def run_one_shot(
    scanner_dir: Path,
    tesseract_path: str,
    poppler_path: str | None,
    logger: logging.Logger,
    confidence_gate_enabled: bool,
    confidence_thresholds: dict[str, float],
    confidence_baseline: float,
) -> int:
    """Executa uma passada única de processamento."""
    checkpoint_file = LOGS_DIR / ".checkpoint"
    processed_files = load_checkpoint(checkpoint_file)

    pdf_files = collect_pending_pdf_files(scanner_dir, processed_files)

    if processed_files:
        logger.info(f"Retomando de checkpoint ({len(processed_files)} ja processados)")

    if not pdf_files:
        logger.info("Nenhum PDF novo encontrado na pasta.")
        return 0

    logger.info(f"Encontrados {len(pdf_files)} arquivos PDF")
    logger.info("-" * 60)

    results = process_pdf_batch(
        pdf_files,
        tesseract_path,
        poppler_path,
        scanner_dir,
        logger,
        processed_files,
        checkpoint_file,
        defer_on_transient=False,
        quarantine_permanent_errors=False,
        max_workers=DEFAULT_WATCH_MAX_WORKERS,
        confidence_gate_enabled=confidence_gate_enabled,
        confidence_thresholds=confidence_thresholds,
        confidence_baseline=confidence_baseline,
    )

    log_batch_summary(logger, results)

    errors = sum(1 for r in results if r.status == ProcessStatus.ERROR)
    deferred = sum(1 for r in results if r.status == ProcessStatus.DEFERRED)

    if errors == 0 and deferred == 0 and len(results) == len(pdf_files):
        clear_checkpoint(checkpoint_file)
        logger.info("Checkpoint limpo - execucao concluida com sucesso!")
    else:
        logger.info(f"Checkpoint salvo - retome a execução para continuar com {errors} erros e {deferred} itens adiados.")

    return errors + deferred


def run_monitor_loop(
    scanner_dir: Path,
    tesseract_path: str,
    poppler_path: str | None,
    logger: logging.Logger,
    watch_interval_seconds: int,
    file_stability_seconds: float,
    file_stability_checks: int,
    deferred_max_attempts: int,
    deferred_retry_cooldown_seconds: int,
    watch_max_workers: int,
    metrics_log_every_cycles: int,
    quarantine_permanent_errors: bool,
    confidence_gate_enabled: bool,
    confidence_thresholds: dict[str, float],
    confidence_baseline: float,
) -> None:
    """Executa monitoramento contínuo da pasta de scanner."""
    checkpoint_file = LOGS_DIR / ".checkpoint"
    heartbeat_file = LOGS_DIR / MONITOR_HEARTBEAT_FILE_NAME
    processed_files = load_checkpoint(checkpoint_file)
    monitor_states: dict[Path, MonitorFileState] = {}
    heartbeat_lock = threading.Lock()
    heartbeat_stop_event = threading.Event()
    heartbeat_tick_seconds = max(10, min(60, watch_interval_seconds))
    heartbeat_state: dict[str, int] = {
        "cycle": 0,
        "pending_files": 0,
        "processed_in_cycle": 0,
        "waiting_copy": 0,
        "waiting_cooldown": 0,
    }

    def _snapshot_heartbeat_state() -> tuple[int, int, int, int, int]:
        with heartbeat_lock:
            return (
                heartbeat_state["cycle"],
                heartbeat_state["pending_files"],
                heartbeat_state["processed_in_cycle"],
                heartbeat_state["waiting_copy"],
                heartbeat_state["waiting_cooldown"],
            )

    def _update_heartbeat_state(
        cycle_number: int,
        pending_files_count: int,
        processed_count: int,
        waiting_copy_count: int,
        waiting_cooldown_count: int,
    ) -> None:
        with heartbeat_lock:
            heartbeat_state["cycle"] = cycle_number
            heartbeat_state["pending_files"] = pending_files_count
            heartbeat_state["processed_in_cycle"] = processed_count
            heartbeat_state["waiting_copy"] = waiting_copy_count
            heartbeat_state["waiting_cooldown"] = waiting_cooldown_count

    def _heartbeat_worker() -> None:
        while not heartbeat_stop_event.wait(heartbeat_tick_seconds):
            (
                hb_cycle,
                hb_pending,
                hb_processed,
                hb_waiting_copy,
                hb_waiting_cooldown,
            ) = _snapshot_heartbeat_state()
            try:
                write_monitor_heartbeat(
                    heartbeat_file=heartbeat_file,
                    cycle_number=hb_cycle,
                    watch_interval_seconds=watch_interval_seconds,
                    pending_files=hb_pending,
                    processed_in_cycle=hb_processed,
                    waiting_copy=hb_waiting_copy,
                    waiting_cooldown=hb_waiting_cooldown,
                )
            except OSError as heartbeat_error:
                logger.warning(f"Falha ao atualizar heartbeat do monitor: {heartbeat_error}")

    heartbeat_thread = threading.Thread(
        target=_heartbeat_worker,
        name="monitor-heartbeat",
        daemon=True,
    )
    heartbeat_thread.start()
    metrics_every = max(1, metrics_log_every_cycles)
    metrics_totals: dict[str, float] = {
        "cycles": 0.0,
        "elapsed_seconds": 0.0,
        "processed": 0.0,
        "deferred": 0.0,
        "review": 0.0,
    }

    logger.info(f"Modo monitor ativo: verificando a cada {watch_interval_seconds}s")
    if processed_files:
        logger.info(f"Checkpoint carregado ({len(processed_files)} arquivos já tratados)")

    idle_cycles = 0
    try:
        while True:
            cycle_start = time.perf_counter()
            pending_files = collect_pending_pdf_files(scanner_dir, processed_files)
            pending_set = set(pending_files)
            for tracked_path in list(monitor_states.keys()):
                if tracked_path not in pending_set:
                    monitor_states.pop(tracked_path, None)

            now = time.time()
            ready_files: list[Path] = []
            waiting_copy = 0
            waiting_cooldown = 0

            for pdf_path in pending_files:
                state = monitor_states.setdefault(pdf_path, MonitorFileState())

                if now < state.next_retry_at:
                    waiting_cooldown += 1
                    state.last_status = "cooldown"
                    continue

                try:
                    stat = pdf_path.stat()
                except (FileNotFoundError, PermissionError, OSError):
                    waiting_copy += 1
                    state.last_status = "unavailable"
                    continue

                changed = (
                    state.last_size != stat.st_size
                    or state.last_mtime_ns != stat.st_mtime_ns
                )

                if changed:
                    state.last_size = stat.st_size
                    state.last_mtime_ns = stat.st_mtime_ns
                    state.stable_hits = 1
                    waiting_copy += 1
                    state.last_status = "copying"
                    continue

                state.stable_hits += 1
                stable_age = now - stat.st_mtime
                if state.stable_hits >= max(1, file_stability_checks) and stable_age >= file_stability_seconds:
                    ready_files.append(pdf_path)
                    state.last_status = "ready"
                else:
                    waiting_copy += 1
                    state.last_status = "stabilizing"

            if ready_files:
                logger.info(f"Encontrados {len(ready_files)} PDFs prontos para processamento")
                logger.info("-" * 60)
                results = process_pdf_batch(
                    ready_files,
                    tesseract_path,
                    poppler_path,
                    scanner_dir,
                    logger,
                    processed_files,
                    checkpoint_file,
                    defer_on_transient=True,
                    quarantine_permanent_errors=quarantine_permanent_errors,
                    max_workers=watch_max_workers,
                    confidence_gate_enabled=confidence_gate_enabled,
                    confidence_thresholds=confidence_thresholds,
                    confidence_baseline=confidence_baseline,
                )

                for result in results:
                    state = monitor_states.setdefault(result.original_path, MonitorFileState())
                    if result.status == ProcessStatus.DEFERRED:
                        state.defer_count += 1
                        state.next_retry_at = time.time() + deferred_retry_cooldown_seconds
                        if state.defer_count >= deferred_max_attempts:
                            checkpoint_key = None
                            try:
                                checkpoint_key = build_checkpoint_key(result.original_path)
                            except FileNotFoundError:
                                checkpoint_key = None
                            quarantined_path = quarantine_failed_pdf(result.original_path, scanner_dir, logger)
                            if quarantined_path is not None:
                                logger.warning(
                                    f"  Limite de adiamentos excedido ({deferred_max_attempts}) para {result.original_path.name}"
                                )
                                if checkpoint_key is not None:
                                    processed_files.add(checkpoint_key)
                                save_checkpoint(processed_files, checkpoint_file)
                                monitor_states.pop(result.original_path, None)
                    elif result.status in {
                        ProcessStatus.RENAMED,
                        ProcessStatus.UNIDENTIFIED,
                        ProcessStatus.SKIPPED,
                        ProcessStatus.QUARANTINED,
                        ProcessStatus.REVIEW,
                    }:
                        monitor_states.pop(result.original_path, None)

                log_batch_summary(logger, results)
                idle_cycles = 0
            else:
                results = []
                if waiting_copy or waiting_cooldown:
                    logger.info(
                        f"Aguardando estabilidade: {waiting_copy} arquivo(s) em copia/instavel, "
                        f"{waiting_cooldown} em cooldown de retry."
                    )
                elif idle_cycles == 0 or idle_cycles % 4 == 0:
                    logger.info("Nenhum PDF novo encontrado.")
                idle_cycles += 1

            cycle_elapsed = time.perf_counter() - cycle_start
            status_counts = _status_counts(results)
            processed_in_cycle = len(results)
            deferred_in_cycle = status_counts[ProcessStatus.DEFERRED]
            review_in_cycle = status_counts[ProcessStatus.REVIEW]

            _update_heartbeat_state(
                cycle_number=int(metrics_totals["cycles"]) + 1,
                pending_files_count=len(pending_files),
                processed_count=processed_in_cycle,
                waiting_copy_count=waiting_copy,
                waiting_cooldown_count=waiting_cooldown,
            )

            metrics_totals["cycles"] += 1
            metrics_totals["elapsed_seconds"] += cycle_elapsed
            metrics_totals["processed"] += processed_in_cycle
            metrics_totals["deferred"] += deferred_in_cycle
            metrics_totals["review"] += review_in_cycle

            should_log_metrics = (
                processed_in_cycle > 0
                or metrics_totals["cycles"] == 1
                or int(metrics_totals["cycles"]) % metrics_every == 0
            )
            if should_log_metrics:
                log_monitor_cycle_metrics(
                    logger,
                    int(metrics_totals["cycles"]),
                    cycle_elapsed,
                    processed_in_cycle,
                    deferred_in_cycle,
                    review_in_cycle,
                    metrics_totals,
                )

            try:
                write_monitor_heartbeat(
                    heartbeat_file=heartbeat_file,
                    cycle_number=int(metrics_totals["cycles"]),
                    watch_interval_seconds=watch_interval_seconds,
                    pending_files=len(pending_files),
                    processed_in_cycle=processed_in_cycle,
                    waiting_copy=waiting_copy,
                    waiting_cooldown=waiting_cooldown,
                )
            except OSError as heartbeat_error:
                logger.warning(f"Falha ao atualizar heartbeat do monitor: {heartbeat_error}")

            time.sleep(watch_interval_seconds)
    except KeyboardInterrupt:
        logger.info("Monitor encerrado pelo usuario.")
    finally:
        heartbeat_stop_event.set()
        heartbeat_thread.join(timeout=2)


def build_arg_parser() -> argparse.ArgumentParser:
    """Cria o parser de argumentos da linha de comando."""
    parser = argparse.ArgumentParser(description="PDF Scanner OCR Document Renamer")
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Mantém o processo em loop e monitora a pasta continuamente.",
    )
    parser.add_argument(
        "--watch-interval",
        type=int,
        default=None,
        help="Intervalo em segundos entre varreduras no modo monitor.",
    )
    return parser


def main():
    args = build_arg_parser().parse_args()

    # Carregar configuracao
    config = load_config()
    scanner_dir = Path(config.get("scanner_dir", str(DEFAULT_SCANNER_DIR)))
    confidence_thresholds = config.get("confidence_thresholds", {})
    confidence_baseline = float(config.get("confidence_baseline", DEFAULT_MIN_CONFIDENCE_BASELINE))
    confidence_gate_enabled = bool(config.get("confidence_gate_enabled", True))

    if not scanner_dir.is_dir():
        print(f"ERRO: Pasta de scanner nao encontrada: {scanner_dir}")
        print(f"Edite o arquivo {CONFIG_FILE} para configurar o caminho correto.")
        sys.exit(1)

    logger = setup_logging()
    logger.info("=" * 60)
    logger.info("  PDF Scanner - OCR Document Renamer")
    logger.info("=" * 60)
    logger.info(f"Pasta de entrada: {scanner_dir}")
    logger.info(f"Projeto: {PROJECT_ROOT}")

    # Validar tessdata
    try:
        validate_tessdata()
    except FileNotFoundError as e:
        logger.error(str(e))
        sys.exit(1)

    # Validar dependencias
    try:
        tesseract_path = find_tesseract_path()
        poppler_path = find_poppler_path()
    except FileNotFoundError as e:
        logger.error(str(e))
        sys.exit(1)

    logger.info(f"Tesseract: {tesseract_path}")
    logger.info(f"Poppler: {poppler_path or 'no PATH'}")
    logger.info(f"Tessdata: {TESSDATA_DIR}")

    # Configurar tesseract_cmd uma unica vez antes de iniciar o processamento paralelo.
    configure_tesseract_command(tesseract_path)

    # Definir TESSDATA_PREFIX para que Tesseract encontre os idiomas
    os.environ["TESSDATA_PREFIX"] = str(TESSDATA_DIR)

    # Validar ambiente (testes de permissoes e funcionalidade)
    try:
        validate_environment(tesseract_path, poppler_path, scanner_dir, logger)
    except RuntimeError as e:
        logger.error(f"Validacao de ambiente falhou: {e}")
        sys.exit(1)

    if args.watch:
        watch_interval = args.watch_interval or int(config.get("watch_interval_seconds", DEFAULT_WATCH_INTERVAL_SECONDS))
        run_monitor_loop(
            scanner_dir,
            tesseract_path,
            poppler_path,
            logger,
            watch_interval,
            float(config.get("file_stability_seconds", DEFAULT_FILE_STABILITY_SECONDS)),
            int(config.get("file_stability_checks", DEFAULT_FILE_STABILITY_CHECKS)),
            int(config.get("deferred_max_attempts", DEFAULT_DEFERRED_MAX_ATTEMPTS)),
            int(config.get("deferred_retry_cooldown_seconds", DEFAULT_DEFERRED_RETRY_COOLDOWN_SECONDS)),
            int(config.get("watch_max_workers", DEFAULT_WATCH_MAX_WORKERS)),
            int(config.get("metrics_log_every_cycles", DEFAULT_METRICS_LOG_EVERY_CYCLES)),
            bool(config.get("quarantine_permanent_errors", True)),
            confidence_gate_enabled,
            confidence_thresholds,
            confidence_baseline,
        )
    else:
        run_one_shot(
            scanner_dir,
            tesseract_path,
            poppler_path,
            logger,
            confidence_gate_enabled,
            confidence_thresholds,
            confidence_baseline,
        )


if __name__ == "__main__":
    main()
