"""
PDF Scanner - OCR Document Renamer
Le PDFs escaneados da pasta SCANNER, identifica tipo de documento
e nome do funcionario via OCR, e renomeia os arquivos.

Uso: python main.py
Configuracao: edite config.ini (criado automaticamente na primeira execucao)
"""

import argparse
import json
import locale
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

try:
    from pypdf import PdfReader, PdfWriter
except ImportError:
    PdfReader = None
    PdfWriter = None

try:
    pytesseract.pytesseract.DEFAULT_ENCODING = "latin-1"
except Exception:
    pass

# RECOMENDAÇÃO 1: Import do monitor de confiança (opcional)
try:
    from monitor_confidence import ConfidenceMonitor, LOW_CONFIDENCE_THRESHOLD
    CONFIDENCE_MONITOR_AVAILABLE = True
    CONFIDENCE_MONITOR_IMPORT_ERROR = None
except ImportError:
    CONFIDENCE_MONITOR_AVAILABLE = False
    LOW_CONFIDENCE_THRESHOLD = 80.0
    CONFIDENCE_MONITOR_IMPORT_ERROR = "monitor_confidence.py nao encontrado; telemetria de baixa confianca desabilitada."


# =============================================================================
# CONFIGURACAO
# =============================================================================

# Caminhos relativos ao projeto (derivados da localizacao do script)
PROJECT_ROOT = Path(__file__).resolve().parent
TESSDATA_DIR = PROJECT_ROOT / "tessdata"
LOGS_DIR = PROJECT_ROOT / "logs"
CONFIG_FILE = PROJECT_ROOT / "config.ini"
DEFAULT_SCANNER_DIR = Path(r"G:\RH\EQUIPE RH\ARQUIVO\SCANNER")

try:
    os.environ.setdefault("TESSDATA_PREFIX", f"{TESSDATA_DIR.resolve().as_posix()}/")
except Exception:
    pass

# OCR
OCR_DPI = 300
OCR_DPI_HIRES = 450  # DPI alto para documentos manuscritos
OCR_LANG = "por"
MAX_PAGES_TO_OCR = None  # Ler TODAS as páginas (sem limite)

# Parâmetros de tuning (podem ser ajustados em scripts de calibração)
UPSCALE_MIN_WIDTH = 2000
PREPROCESS_ADAPTIVE_BLOCK_SIZE = 31
PREPROCESS_ADAPTIVE_C = 10
PREPROCESS_MEDIAN_BLUR = 3
CLAHE_CLIP_LIMIT = 2.0
CLAHE_TILE_GRID = (8, 8)
TITLE_HINT_MIN_RATIO = 0.84


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        import sys
        print(f"[OCR.CONFIG][WARN] Invalid integer for {name}={raw!r}; using default={default}", file=sys.stderr)
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        import sys
        print(f"[OCR.CONFIG][WARN] Invalid float for {name}={raw!r}; using default={default}", file=sys.stderr)
        return default


def _env_tuple_int(name: str, default: tuple[int, int]) -> tuple[int, int]:
    raw = os.getenv(name)
    if raw is None:
        return default
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) != 2:
        return default
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return default


# Logging wrapper for environment variable loading
_LOADED_ENV_VARS: dict[str, int | float] = {}


def _env_int_with_logging(name: str, default: int) -> int:
    value = _env_int(name, default)
    if os.getenv(name) is not None:
        _LOADED_ENV_VARS[name] = value
    return value


def _env_float_with_logging(name: str, default: float) -> float:
    value = _env_float(name, default)
    if os.getenv(name) is not None:
        _LOADED_ENV_VARS[name] = value
    return value


OCR_DPI = _env_int_with_logging("OCR_TUNE_DPI", OCR_DPI)
OCR_DPI_HIRES = _env_int_with_logging("OCR_TUNE_DPI_HIRES", OCR_DPI_HIRES)
UPSCALE_MIN_WIDTH = _env_int_with_logging("OCR_TUNE_MIN_WIDTH", UPSCALE_MIN_WIDTH)
PREPROCESS_ADAPTIVE_BLOCK_SIZE = _env_int_with_logging("OCR_TUNE_BLOCK_SIZE", PREPROCESS_ADAPTIVE_BLOCK_SIZE)
PREPROCESS_ADAPTIVE_C = _env_int_with_logging("OCR_TUNE_ADAPTIVE_C", PREPROCESS_ADAPTIVE_C)
PREPROCESS_MEDIAN_BLUR = _env_int_with_logging("OCR_TUNE_MEDIAN", PREPROCESS_MEDIAN_BLUR)
CLAHE_CLIP_LIMIT = _env_float_with_logging("OCR_TUNE_CLAHE_CLIP", CLAHE_CLIP_LIMIT)
CLAHE_TILE_GRID = _env_tuple_int("OCR_TUNE_CLAHE_GRID", CLAHE_TILE_GRID)
TITLE_HINT_MIN_RATIO = _env_float_with_logging("OCR_TUNE_TITLE_HINT_RATIO", TITLE_HINT_MIN_RATIO)

# Validacao de bounds para parametros criticos de DPI
if OCR_DPI < 100:
    OCR_DPI = 100
if OCR_DPI > 600:
    OCR_DPI = 600

if OCR_DPI_HIRES < 150:
    OCR_DPI_HIRES = 150
if OCR_DPI_HIRES > 600:
    OCR_DPI_HIRES = 600

# Validacao de bounds para upscaling
if UPSCALE_MIN_WIDTH < 500:
    UPSCALE_MIN_WIDTH = 500
if UPSCALE_MIN_WIDTH > 10000:
    UPSCALE_MIN_WIDTH = 10000

# Validacao de bounds para CLAHE
if CLAHE_CLIP_LIMIT < 1.0:
    CLAHE_CLIP_LIMIT = 1.0
if CLAHE_CLIP_LIMIT > 4.0:
    CLAHE_CLIP_LIMIT = 4.0

# Validacao de bounds para adaptive thresholding
if PREPROCESS_ADAPTIVE_C < 0:
    PREPROCESS_ADAPTIVE_C = 0
if PREPROCESS_ADAPTIVE_C > 100:
    PREPROCESS_ADAPTIVE_C = 100

# Garantias para parâmetros sensíveis do OpenCV (kernel sizes)
if PREPROCESS_ADAPTIVE_BLOCK_SIZE < 3:
    PREPROCESS_ADAPTIVE_BLOCK_SIZE = 3
if PREPROCESS_ADAPTIVE_BLOCK_SIZE % 2 == 0:
    PREPROCESS_ADAPTIVE_BLOCK_SIZE += 1

if PREPROCESS_MEDIAN_BLUR < 1:
    PREPROCESS_MEDIAN_BLUR = 1
if PREPROCESS_MEDIAN_BLUR % 2 == 0:
    PREPROCESS_MEDIAN_BLUR += 1

if TITLE_HINT_MIN_RATIO < 0.5:
    TITLE_HINT_MIN_RATIO = 0.5
if TITLE_HINT_MIN_RATIO > 0.99:
    TITLE_HINT_MIN_RATIO = 0.99

# Log das variaveis de ambiente carregadas (para debug de tuning)
if _LOADED_ENV_VARS:
    import sys
    print(f"[OCR.CONFIG] Loaded tuning parameters from environment: {_LOADED_ENV_VARS}", file=sys.stderr)

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
_MBV_TEMPLATE_WARNING_EMITTED = False

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
    "DECLARACAO",
    "DECLARAÇÃO",
    "RECIBO",
    "COMPROVANTE",
    "CONTRATO",
    "NOTA FISCAL",
    "DANFE",
    "DEVIDOS FINS",
    "OS DEVIDOS FINS",
    "PARA OS DEVIDOS FINS",
    "ATESTADO",
    "SOLICITACAO",
    "SOLICITAÇÃO",
    "ABERTURA DE VAGA",
    "CANDIDATO",
    "PACIENTE",
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
        "RESPONSAVEL",
        "RESPONSÁVEL",
        "RESPONS",
        "OU RESPONS",
        "OU RESPONSAVEL",
        "OU RESPONSÁVEL",
        "OU RESPONSÃ",
        "DEVIDOS",
        "FINS",
        "OS DEVIDOS FINS",
        "PARA OS DEVIDOS FINS",
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
    "TREINAMENTO": {
        "TREINAMENTO",
        "PARTICIPANTE",
        "INSTRUTOR",
        "CARGA HORARIA",
        "CARGA HORÁRIA",
        "CENTRAL",
        "REGISTRO",
        "LISTA",
        "PRESENCA",
        "PRESENÇA",
        "APONTAMENTOS",
        "QUE DEVE",
        "DEVE FAZER",
    },
    "PAPELETA_CONTROLE_JORNADA": {
        "PAPELETA",
        "CONTROLE",
        "JORNADA",
        "PONTO",
        "HORARIO",
        "HORÁRIO",
    },
    "PAPELETA": {
        "PAPELETA",
        "JORNADA",
        "HORARIO",
        "HORÁRIO",
        "INTERVALO",
        "REPOUSO",
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
    "NF": {
        "NOTA",
        "FISCAL",
        "DANFE",
        "CHAVE",
        "ACESSO",
        "EMITENTE",
        "DESTINATARIO",
        "DESTINATÁRIO",
        "DOCUMENTO AUXILIAR",
        "VALOR TOTAL",
    },
    "RECIBO": {
        "RECIBO",
        "RECEBI",
        "RECEBEMOS",
        "VALOR",
        "R$",
        "REFERENCIA",
        "REFERÊNCIA",
        "IMPORTANCIA",
        "IMPORTÂNCIA",
    },
    "DECLARACAO": {
        "DECLARACAO",
        "DECLARAÇÃO",
        "DECLARO",
        "ATESTO",
        "DEVIDOS FINS",
        "ASSINATURA",
        "CPF",
        "RG",
    },
    "CONTRATO": {
        "CONTRATO",
        "CONTRATANTE",
        "CONTRATADO",
        "CLAUSULA",
        "CLÁUSULA",
        "VIGENCIA",
        "VIGÊNCIA",
    },
    "COMPROVANTE": {
        "COMPROVANTE",
        "PAGAMENTO",
        "TRANSFERENCIA",
        "TRANSFERÊNCIA",
        "PROTOCOLO",
        "AUTENTICACAO",
        "AUTENTICAÇÃO",
        "BANCO",
    },
    "RELATORIO_ABASTECIMENTO": {
        "ABASTECIMENTO",
        "RELATORIO",
        "RELATÓRIO",
        "DESTINOS",
        "MOTORISTA",
        "LITRO",
        "KM",
    },
    "ABERTURA_VAGA": {
        "ABERTURA",
        "VAGA",
        "CARGO",
        "REQUISITOS",
        "REQUISITO",
        "SELECAO",
        "SELEÇÃO",
        "RECRUTAMENTO",
        "DESCRICAO",
        "DESCRIÇÃO",
    },
    "DUT_DECLARACAO": {
        "required": [
            r"\bDUT\b",
            r"DECLARA.{0,4}O",
        ],
        "optional": [
            r"PROPRIET[ÁA]RIO",
            r"COMPRADOR",
            r"VENDEDOR",
        ],
    },
    "POLITICA_VIOLACOES_VELOCIDADE": {
        "required": [
            r"POL[ÍI]TICA",
            r"VIOLA.{0,4}ES",
            r"VELOCIDADE",
        ],
        "optional": [
            r"CONDUTOR",
            r"MOTORISTA",
            r"TRANSPORTADORA",
        ],
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
            r"(?:[Ff]echamento\s+[Mm]ensal|[Rr]elat.?rio\s+de\s+[Rr]eembolso\s+de\s+[Dd]espesas\s+de\s+[Vv]iagens|[Ff]echamento\s*:\s*\d+)",
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
            r"[Rr]eembolso",
            r"[Dd]espesas\s+de\s+[Vv]iagens",
            r"[Aa]diantamentos?",
        ],
    },
    "RELATORIO_ABASTECIMENTO": {
        "required": [
            r"[Aa]bastec(?:imento|imen|im|)\w*",
            r"(?:\bKM\b|[Ll]itro|[Rr]\$|[Dd]estinos?)",
        ],
        "optional": [
            r"[Rr]ondon[oó]polis",
            r"[Pp]aranag[uú]a",
            r"[Rr]io\s+[Vv]erde",
            r"[Nn]ota\s*(?:fiscal|fatura)",
            r"[Mm]otorista",
            r"[Rr]elat.?rio",
        ],
    },
    "SOLICITACAO_CONTRATACAO": {
        "required": [
            r"\bRE\s*:\s*MP\s*-\s*[Cc]ontrata",
            r"[Aa]utorizad[oa]",
        ],
        "optional": [
            r"[Cc]ontrata(?:r|[çc][ãa]o)",
            r"[Rr]ecrutamento",
            r"[Pp]ediu\s+demiss",
            r"[Pp]restar\s+servi[çc]o",
            r"[Oo]utlook",
        ],
    },
    "ABERTURA_VAGA": {
        "required": [
            r"[Aa]bertura\s+de\s+[Vv]aga",
            r"(?:[Cc]argo|[Vv]aga|[Pp]osi[çc][ãa]o)",
        ],
        "optional": [
            r"[Cc]andidato",
            r"[Rr]equisi(?:tos|to)",
            r"[Rr]ecrutamento",
            r"[Ss]ele[çc][ãa]o",
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
    "ADVERTENCIA_ESCRITA": {
        "required": [
            r"ADVERT[ÊE]NCIA\s+ESCRITA",
            r"COLABORADOR",
        ],
        "optional": [
            r"TRANSPORTADORA",
            r"CPF",
            r"[Dd]isciplinar",
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
            r"[Dd]eclara.{0,4}o",
            r"(?:[Rr]acial|[Ee]tnic[oa])",
        ],
        "optional": [
            r"[Aa]utodeclara[çc][ãa]o",
            r"[Aa]utodeclara.{0,4}o",
            r"[Rr]a[çc][aa]",
            r"[Ee]tnic",
            r"[Ee]tnic[oa]\s+[Rr]acial",
        ],
    },
    "ALTERACAO_BENEFICIARIOS": {
        "required": [
            r"ICATU",
            r"BENEFICI[ÁA]RI",
            r"(?:ALTERA[ÇC][ÃA]O|INDICA[ÇC][ÃA]O)",
        ],
        "optional": [
            r"SEGUROS",
            r"ALTERA[ÇC][ÃA]O/INDICA[ÇC][ÃA]O",
            r"DADOS\s+PESSOAIS",
            r"PLANO",
        ],
    },
    "NF": {
        "required": [
            r"NOTA\s+[FP]IS?CAL",
            r"(?:DANFE|CHAVE\s+DE\s+ACESSO|DOCUMENTO\s+AUXILIAR)",
        ],
        "optional": [
            r"ELETR.?NICA",
            r"CHAVE\s+DE\s+ACESSO",
            r"DOCUMENTO\s+AUXILIAR",
            r"DANFE",
            r"NCM",
            r"CFOP",
        ],
    },
    "RECIBO": {
        "required": [
            r"RECIBO",
            r"(?:RECEB[EI]M?OS?|VALOR|R\$)",
        ],
        "optional": [
            r"RECEB[EI]",
            r"VALOR",
            r"R\$",
            r"REFER[ÊE]NCIA",
            r"ASSINATURA",
        ],
    },
    "DECLARACAO": {
        "required": [
            r"DECLARA.{0,4}O",
            r"(?:DECLARO|ATESTO|PARA\s+OS\s+DEVIDOS\s+FINS)",
        ],
        "optional": [
            r"DECLARO",
            r"ATESTO",
            r"PARA\s+OS\s+DEVIDOS\s+FINS",
            r"CPF",
            r"ASSINATURA",
        ],
    },
    "CONTRATO": {
        "required": [
            r"CONTRATO",
            r"(?:CONTRATANTE|CONTRATADO|CL[ÁA]USULA|VIG[ÊE]NCIA)",
        ],
        "optional": [
            r"CONTRATANTE",
            r"CONTRATADO",
            r"CL[ÁA]USULA",
            r"VIG[ÊE]NCIA",
            r"OBJETO",
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
            r"BANCO",
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
    "ADVERTENCIA_ESCRITA": ["ADVERTENCIA ESCRITA", "ADVERTENCIA DISCIPLINAR"],
    "DECLARACAO_RACIAL": [
        "DECLARACAO RACIAL",
        "AUTODECLARACAO RACIAL",
        "AUTODECLARACAO ETNICO RACIAL",
        "AUTODECLARAÇÃO ÉTNICO RACIAL",
        "AUTODECLARA O ETNICO RACIAL",
    ],
    "DECLARACAO": [
        "DECLARACAO DE ULTIMO DIA DE TRABALHADO",
        "ULTIMO DIA DE TRABALHADO",
        "DECLARAÇÃO DE ULTIMO DIA DE TRABALHADO",
    ],
    "RELATORIO_ABASTECIMENTO": [
        "RELATORIO DE REEMBOLSO DE DESPESAS DE VIAGENS",
        "ABASTECIMENTO",
    ],
    "SOLICITACAO_CONTRATACAO": [
        "RE: MP - CONTRATACAO",
        "MP - CONTRATACAO",
    ],
    "ABERTURA_VAGA": [
        "ABERTURA DE VAGA",
        "REQUISICAO DE VAGA",
    ],
    "DUT_DECLARACAO": [
        "DUT DECLARACAO",
    ],
    "POLITICA_VIOLACOES_VELOCIDADE": [
        "POLITICA DE VIOLACOES VELOCIDADE",
        "POLITICA VIOLACOES VELOCIDADE",
    ],
    "ALTERACAO_BENEFICIARIOS": [
        "ICATU SEGUROS ALTERACAO INDICACAO DE BENEFICIARIOS",
        "ALTERACAO INDICACAO DE BENEFICIARIOS",
    ],
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
    "AP": 75,
    "ADVERTENCIA_ESCRITA": 74,
    "TREINAMENTO_DIRECAO_DEFENSIVA": 74,
    "TESTE_CONHECIMENTOS_GERAIS": 73,
    "TESTE_PRATICO": 72,
    "PAPELETA_CONTROLE_JORNADA": 71,
    "QUESTIONARIO_ACOLHIMENTO": 70,
    "DECLARACAO_RACIAL": 76,
    "ALTERACAO_BENEFICIARIOS": 75,
    "DUT_DECLARACAO": 74,
    "POLITICA_VIOLACOES_VELOCIDADE": 73,
    "CONTRATO": 70,
    "DECLARACAO": 65,
    "RELATORIO_ABASTECIMENTO": 64,
    "SOLICITACAO_CONTRATACAO": 63,
    "ABERTURA_VAGA": 62,
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


def _line_matches_phrase(ocr_line: str, phrase: str, min_ratio: float = TITLE_HINT_MIN_RATIO) -> bool:
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
            if _line_matches_phrase(line, phrase, min_ratio=TITLE_HINT_MIN_RATIO):
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
    "AUTODECLARA O ETNICO RACIAL": "AUTODECLARAÇÃO ÉTNICO RACIAL",
    "AUTODECLARACAO ETNICO RACIAL": "AUTODECLARAÇÃO ÉTNICO RACIAL",
    "RESPONSÃ": "RESPONSÁVEL",
    "RESPONSAVEL": "RESPONSÁVEL",
    "DECLARAÇAO": "DECLARAÇÃO",
    "DECLARACAO DE": "DECLARAÇÃO DE",
    "COMPETENCIA": "COMPETÊNCIA",
    "REFERENCIA": "REFERÊNCIA",
    "EMISSAO": "EMISSÃO",
    "AUTENTICACAO": "AUTENTICAÇÃO",
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


def configure_tesseract_environment() -> None:
    """Configura TESSDATA_PREFIX para a pasta tessdata do projeto.

    Neste ambiente, o Tesseract resolve os idiomas diretamente a partir de
    ``TESSDATA_PREFIX`` apontando para a pasta tessdata.
    """
    desired_prefix = f"{TESSDATA_DIR.resolve().as_posix()}/"
    if os.environ.get("TESSDATA_PREFIX") != desired_prefix:
        os.environ["TESSDATA_PREFIX"] = desired_prefix


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

MONTH_NAME_MAP = {
    "JAN": "01", "JANEIRO": "01",
    "FEV": "02", "FEVEREIRO": "02",
    "MAR": "03", "MARCO": "03", "MARÇO": "03",
    "ABR": "04", "ABRIL": "04",
    "MAI": "05", "MAIO": "05",
    "JUN": "06", "JUNHO": "06",
    "JUL": "07", "JULHO": "07",
    "AGO": "08", "AGOSTO": "08",
    "SET": "09", "SETEMBRO": "09",
    "OUT": "10", "OUTUBRO": "10",
    "NOV": "11", "NOVEMBRO": "11",
    "DEZ": "12", "DEZEMBRO": "12",
}


def _ocr_digits(value: str) -> str:
    """Converte confusões comuns de OCR em dígitos (ex.: O->0, I->1)."""
    table = str.maketrans({
        "O": "0", "o": "0", "Q": "0", "D": "0",
        "I": "1", "l": "1", "|": "1",
        "S": "5", "s": "5",
        "B": "8",
        "Z": "2", "z": "2",
        "G": "6", "g": "6",
    })
    return re.sub(r"\D", "", value.translate(table))


def _normalize_date_parts(day: str, month: str, year: str) -> str | None:
    """Normaliza e valida dia/mes/ano para DD-MM-YYYY."""
    d_raw = _ocr_digits(day)
    m_raw = _ocr_digits(month)
    y_raw = _ocr_digits(year)

    if not d_raw or not m_raw or not y_raw:
        return None

    d = int(d_raw)
    m = int(m_raw)
    if not (1 <= d <= 31 and 1 <= m <= 12):
        return None

    yyyy = correct_year(y_raw)
    return f"{d:02d}-{m:02d}-{yyyy}"


def _month_name_to_number(token: str) -> str | None:
    normalized = unicodedata.normalize("NFKD", token)
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    normalized = re.sub(r"[^A-Za-z]", "", normalized).upper()
    return MONTH_NAME_MAP.get(normalized)


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

    # Faixa razoavel: preserva anos historicos reais e permite correcoes em anos futuros/fora da faixa.
    min_year = 1900
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

    # Nenhuma correcao possivel -- preservar o valor original quando ele ja parece ser um ano de 4 digitos.
    if len(year_str) == 4 and 1900 <= year_int <= 2100:
        return year_str

    # Ultimo recurso para valores truncados ou inconsistentes.
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


def _upscale_if_small(gray: np.ndarray, min_width: int = UPSCALE_MIN_WIDTH) -> np.ndarray:
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
        cv2.THRESH_BINARY, PREPROCESS_ADAPTIVE_BLOCK_SIZE, PREPROCESS_ADAPTIVE_C,
    )

    # Reducao de ruido
    denoised = cv2.medianBlur(binary, PREPROCESS_MEDIAN_BLUR)
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

def preprocess_image_enhanced(pil_image: Image.Image) -> Image.Image:
    """Preprocessamento mais forte para documentos de baixa legibilidade.

    Aplica equalizacao local, suavizacao leve e limpeza morfologica para
    reforcar traços fracos sem destruir textos pequenos.
    """
    gray = _to_gray(pil_image)
    gray = _upscale_if_small(gray)
    gray = deskew_image(gray)
    gray = _apply_clahe(gray)

    # Reduz ruido sem borrar excessivamente bordas de letra.
    gray = cv2.bilateralFilter(gray, 7, 45, 45)

    binary = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY, PREPROCESS_ADAPTIVE_BLOCK_SIZE, PREPROCESS_ADAPTIVE_C,
    )

    # Cleanup morfologico leve: remove pontos isolados e fecha pequenas falhas.
    kernel = np.ones((2, 2), np.uint8)
    binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel, iterations=1)
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel, iterations=1)

    if PREPROCESS_MEDIAN_BLUR > 1:
        binary = cv2.medianBlur(binary, PREPROCESS_MEDIAN_BLUR)

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
    clahe = cv2.createCLAHE(clipLimit=CLAHE_CLIP_LIMIT, tileGridSize=CLAHE_TILE_GRID)
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


def log_optional_runtime_warnings(logger: logging.Logger) -> None:
    """Registra dependencias opcionais ausentes e modos degradados."""
    if not CONFIDENCE_MONITOR_AVAILABLE and CONFIDENCE_MONITOR_IMPORT_ERROR:
        logger.warning(CONFIDENCE_MONITOR_IMPORT_ERROR)

    missing_templates = [
        filename
        for filename in MBV_TEMPLATE_FILES.values()
        if not (MBV_TEMPLATE_DIR / filename).is_file()
    ]
    if missing_templates:
        logger.warning(
            "Templates MBV ausentes em %s; extracao MBV seguira em modo degradado (ROI sem template). Faltando: %s",
            MBV_TEMPLATE_DIR,
            ", ".join(missing_templates),
        )


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
    configure_tesseract_environment()

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
    """Extrai data em múltiplos formatos e normaliza para DD-MM-YYYY."""
    # Formatos numéricos: DD/MM/YYYY e DD-MM-YYYY (inclui 1 dígito)
    for match in re.finditer(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{1,4})\b", text):
        normalized = _normalize_date_parts(match.group(1), match.group(2), match.group(3))
        if normalized:
            return normalized

    # Formato textual: DD de MÊS de YYYY
    textual = re.search(
        r"\b(\d{1,2})\s+de\s+([A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç]+)\s+de\s+(\d{2,4})\b",
        text,
        re.IGNORECASE,
    )
    if textual:
        month_num = _month_name_to_number(textual.group(2))
        if month_num:
            normalized = _normalize_date_parts(textual.group(1), month_num, textual.group(3))
            if normalized:
                return normalized

    # Competência textual: mês conhecido/YYYY => MM-YYYY
    month_year_textual = re.search(
        r"\b((?:JAN|FEV|MAR|ABR|MAI|JUN|JUL|AGO|SET|OUT|NOV|DEZ)[A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç]*)\s*/\s*(\d{2,4})\b",
        text,
        re.IGNORECASE,
    )
    if month_year_textual:
        month_num = _month_name_to_number(month_year_textual.group(1))
        if month_num:
            yyyy = correct_year(_ocr_digits(month_year_textual.group(2)) or month_year_textual.group(2))
            return f"{month_num}-{yyyy}"

    # Competência numérica: MM/YYYY => MM-YYYY
    month_year_num = re.search(r"\b(\d{1,2})[/-](\d{2,4})(?![/-]\d)\b", text)
    if month_year_num:
        m = _ocr_digits(month_year_num.group(1))
        y = _ocr_digits(month_year_num.group(2))
        if m and y:
            month_int = int(m)
            if 1 <= month_int <= 12:
                return f"{month_int:02d}-{correct_year(y)}"

    return None


RECENT_OPERATIONAL_DOC_TYPES = {
    "ADVERTENCIA_ESCRITA",
    "AVALIACAO_MOTORISTA",
    "TESTE_PRATICO",
    "TESTE_CONHECIMENTOS_GERAIS",
    "TREINAMENTO_DIRECAO_DEFENSIVA",
    "TREINAMENTO",
    "PAPELETA_CONTROLE_JORNADA",
    "PAPELETA",
    "QUESTIONARIO_ACOLHIMENTO",
    "POLITICA_VIOLACOES_VELOCIDADE",
}


def _is_suspicious_period(period: str | None, doc_type: str | None, confidence_score: float = 100.0) -> bool:
    """Identifica datas provavelmente falsas em OCR de baixa qualidade."""
    if not period or period in {"SEM DATA", "SEM PERIODO"}:
        return False

    match = re.fullmatch(r"(\d{2})-(\d{2})-(\d{4})", period.strip())
    if not match:
        return False

    day = int(match.group(1))
    month = int(match.group(2))
    year = int(match.group(3))
    current_year = datetime.now().year

    if not (1 <= day <= 31 and 1 <= month <= 12):
        return True

    if year > current_year + 1:
        return True

    if doc_type in RECENT_OPERATIONAL_DOC_TYPES and confidence_score < 60.0 and year < current_year - 5:
        return True

    return False


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


def _extract_name_from_labeled_lines(text: str, doc_type: str) -> str | None:
    """Extrai nome a partir de rótulos comuns em linha atual/próxima linha.

    Exemplos cobertos:
    - "Empregado: JOAO DA SILVA"
    - "Assinatura do empregado:\nJOAO DA SILVA"
    - "Nome completo - JOAO DA SILVA"
    """
    label_pattern = re.compile(
        r"(?i)\b(?:nome(?:\s+completo)?|empregado(?:\(a\))?|colaborador(?:\(a\))?|"
        r"funcion[áa]rio(?:\(a\))?|candidato(?:\(a\))?|paciente|titular|"
        r"assinatura\s+do\s+(?:empregado|colaborador|funcion[áa]rio|candidato|paciente|titular))\b"
    )

    lines = text.splitlines()
    for idx, raw_line in enumerate(lines):
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line:
            continue

        match = label_pattern.search(line)
        if not match:
            continue

        tail = line[match.end():]
        tail = re.sub(r"^[\s:\-–—.]+", "", tail).strip()

        candidate = tail
        if not candidate:
            for j in range(idx + 1, min(idx + 4, len(lines))):
                next_line = re.sub(r"\s+", " ", lines[j]).strip()
                if next_line:
                    candidate = next_line
                    break

        if not candidate:
            continue

        candidate = re.sub(r"\s*(CPF|CTPS|RG|DATA|TELEFONE|ENDERECO|ENDEREÇO|EMAIL).*$", "", candidate, flags=re.IGNORECASE)
        cleaned = clean_name(candidate)
        if not cleaned:
            continue

        normalized_no_accents = unicodedata.normalize("NFKD", cleaned)
        normalized_no_accents = "".join(ch for ch in normalized_no_accents if not unicodedata.combining(ch))
        normalized_no_accents = normalized_no_accents.upper()

        if re.search(r"\b(MEDIC|ODONTO|CRM|CRO|CARIMBO|RESPONSAVEL|RESPONSAVEL\b)\b", normalized_no_accents):
            continue

        # Evita capturar somente rótulos em vez de nome real.
        if re.search(r"\b(ASSINATURA|EMPREGADO|EMPREGADOR|COLABORADOR|FUNCIONARIO|FUNCIONÁRIO|NOME|PACIENTE|CANDIDATO|TITULAR)\b", cleaned):
            continue

        if _is_valid_name_for_doc_type(cleaned, doc_type):
            return cleaned

    return None


def _extract_name_from_filename(pdf_path: Path, doc_type: str | None) -> str | None:
    """Extrai nome de pessoa a partir do nome do arquivo em formatos comuns.

    Usa abordagem conservadora para evitar capturar cargo/tipo de documento.
    """
    stem = pdf_path.stem
    if not stem:
        return None

    normalized = stem.upper()
    if any(term in normalized for term in ("NAO IDENTIFICADO", "NÃO IDENTIFICADO", "SEM NOME", "NOME NAO LOCALIZADO", "ILEGIVEL", "ILLEGIVEL")):
        return None

    role_words = {
        "ASSISTENTE", "AUXILIAR", "ANALISTA", "COORDENADOR", "GERENTE",
        "CONTROLE", "JORNADA", "VAGA", "JOVEM", "APRENDIZ", "SEMINOVOS",
    }

    # Tentativa 1: "TIPO - NOME - DATA"
    explicit = re.search(
        r"^[^-]{3,}\s*-\s*([A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç\s]{6,})\s*-\s*(?:\d{2}-\d{2}-\d{1,4}|\d{2}-\d{1,4}).*$",
        stem,
        re.IGNORECASE,
    )
    if explicit:
        candidate = clean_name(explicit.group(1))
        upper_candidate = candidate.upper()
        if not any(word in upper_candidate.split() for word in role_words):
            validation_type = doc_type or "GEN"
            if _is_valid_name_for_doc_type(candidate, validation_type):
                return candidate

    # Tentativa 2: avaliar segmentos separados por " - "
    parts = [p.strip() for p in re.split(r"\s+-\s+", stem) if p.strip()]
    for part in parts:
        if re.search(r"\d", part):
            continue
        candidate = clean_name(part)
        if not candidate:
            continue
        upper_candidate = candidate.upper()
        if any(word in upper_candidate.split() for word in role_words):
            continue
        validation_type = doc_type or "GEN"
        if _is_valid_name_for_doc_type(candidate, validation_type):
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
        if len(only_letters) < 3:
            return False
        normalized_word = unicodedata.normalize("NFKD", only_letters)
        normalized_word = "".join(ch for ch in normalized_word if not unicodedata.combining(ch))
        if not re.search(r"[AEIOU]", normalized_word):
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
    global _MBV_TEMPLATE_WARNING_EMITTED
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
            if not _MBV_TEMPLATE_WARNING_EMITTED:
                logger.warning(
                    "MBV ROI: templates ausentes; usando fallback direto por ROI. Acuracia de MBV pode ficar reduzida."
                )
                _MBV_TEMPLATE_WARNING_EMITTED = True
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
    Usa TESSDATA_PREFIX para localizar o tessdata de forma mais robusta no Windows."""
    return f'--oem 1 --psm {psm}'


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
    configure_tesseract_command(tesseract_path)
    configure_tesseract_environment()

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
    # Se MAX_PAGES_TO_OCR é None, processa TODAS as páginas; caso contrário, limita
    pages_to_process = images if MAX_PAGES_TO_OCR is None else images[:MAX_PAGES_TO_OCR]
    
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
            # Se MAX_PAGES_TO_OCR é None, processa TODAS as páginas
            pages_to_process_hires = hi_res_images if MAX_PAGES_TO_OCR is None else hi_res_images[:MAX_PAGES_TO_OCR]
            for i, img in enumerate(pages_to_process_hires):
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
        # Se MAX_PAGES_TO_OCR é None, processa TODAS as páginas
        pages = images if MAX_PAGES_TO_OCR is None else images[:MAX_PAGES_TO_OCR]

        attempts: list[tuple[str, list[Image.Image], int]] = [
            ("dpi300_psm3_light", [preprocess_image_light(img) for img in pages], 3),
            ("dpi300_psm3_enhanced", [preprocess_image_enhanced(img) for img in pages], 3),
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

    logger.info("  Two-pass fallback final: tentando 600 DPI")
    text_600, type_600 = extract_text_from_pdf(
        pdf_path,
        tesseract_path,
        poppler_path,
        logger,
        base_dpi=600,
    )

    if type_600 is not None:
        logger.info(f"  Two-pass: classificacao recuperada em 600 DPI ({type_600})")
        return text_600, type_600

    quality_600 = _text_quality_ratio(text_600)
    if quality_600 > quality_300:
        logger.debug(f"  Two-pass: mantendo texto 600 DPI por qualidade ({quality_300:.0%} -> {quality_600:.0%})")
        return text_600, None

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
    name = re.sub(r'\s+', ' ', name).strip()
    parts = [part for part in name.split(" ") if part]
    if parts:
        last_part_ascii = unicodedata.normalize("NFKD", parts[-1])
        last_part_ascii = "".join(ch for ch in last_part_ascii if not unicodedata.combining(ch))
        last_part_ascii = re.sub(r"[^A-Z]", "", last_part_ascii.upper())
        if 0 < len(last_part_ascii) <= 2 and parts[-1] not in MBV_ALLOWED_SMALL_WORDS:
            parts = parts[:-1]
            name = " ".join(parts)

    if len(name) > 60:
        name = name[:60].rsplit(' ', 1)[0]
    return name


def _correct_date_in_period(date_str: str) -> str:
    """Aplica correct_year a datas no formato DD/MM/YYYY ou DD-MM-YYYY."""
    # Tentar extrair ano de diferentes formatos
    normalized = _extract_date_from_text(date_str)
    return normalized or "SEM DATA"


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

    if not result["name"]:
        contextual_name = _extract_name_from_labeled_lines(text, doc_type)
        if contextual_name:
            result["name"] = contextual_name

    if not result["name"] and fallback_name:
        result["name"] = fallback_name

    if date_patterns:
        for pattern in date_patterns:
            match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE | re.DOTALL)
            if not match:
                continue

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
        fallback_name=None,
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


def _extract_recent_operational_doc_data(
    text: str,
    doc_type: str,
    extra_name_patterns: list[str] | None = None,
    extra_date_patterns: list[str] | None = None,
    allow_fallback_name: bool = True,
    use_generic_uppercase_pattern: bool = True,
    extra_name_patterns_only: bool = False,
) -> dict:
    """Extrai dados de formularios operacionais recentes com heuristicas conservadoras."""
    name_patterns = [
        r"(?:[Nn]ome(?:\s+completo)?|[Cc]olaborador(?:\(a\))?|[Ee]mpregado(?:\(a\))?|[Cc]andidato|[Cc]ondutor|[Mm]otorist[ao]|[Tt]rabalhador)\s*[:\-]?\s*([A-Za-zÃÃ‰ÃÃ“ÃšÃ‚ÃŠÃŽÃ”Ã›ÃƒÃ•Ã‡Ã¡Ã©Ã­Ã³ÃºÃ¢ÃªÃ®Ã´Ã»Ã£ÃµÃ§\s]{6,})",
    ]
    if extra_name_patterns_only:
        name_patterns = []
    if use_generic_uppercase_pattern:
        name_patterns.append(
            r"\b([A-ZÃÃ‰ÃÃ“ÃšÃ‚ÃŠÃŽÃ”Ã›ÃƒÃ•Ã‡]{3,}(?:\s+[A-ZÃÃ‰ÃÃ“ÃšÃ‚ÃŠÃŽÃ”Ã›ÃƒÃ•Ã‡]{3,}){1,6})\b"
        )
    if extra_name_patterns:
        name_patterns = extra_name_patterns + name_patterns

    date_patterns = [
        r"(?:[Dd]ata|[Ee]miss[Ã£a]o|[Aa]valia[Ã§c][Ã£a]o|[Aa]plica[Ã§c][Ã£a]o|[Cc]ertifica[Ã§c][Ã£a]o|[Pp]er[Ã­i]odo|[Cc]ompet[Ãªe]ncia)\s*[:\-]?\s*(\d{2})/(\d{2})/(\d{1,4})",
        r"(?:[Dd]ata|[Ee]miss[Ã£a]o|[Aa]valia[Ã§c][Ã£a]o|[Aa]plica[Ã§c][Ã£a]o|[Cc]ertifica[Ã§c][Ã£a]o|[Pp]er[Ã­i]odo|[Cc]ompet[Ãªe]ncia)\s*[:\-]?\s*(\d{2})-(\d{2})-(\d{1,4})",
        r"(\d{2})/(\d{2})/(\d{1,4})",
    ]
    if extra_date_patterns:
        date_patterns = extra_date_patterns + date_patterns

    result = _extract_named_doc_data(
        text,
        doc_type,
        name_patterns=name_patterns,
        date_patterns=date_patterns,
        fallback_name=None,
    )

    if allow_fallback_name and not result.get("name"):
        fallback = extract_fallback_data(text)
        fallback_name = fallback.get("name")
        if fallback_name and _is_valid_name_for_doc_type(fallback_name, doc_type):
            result["name"] = fallback_name

    if result.get("period") and _is_suspicious_period(result["period"], doc_type, confidence_score=55.0):
        result["period"] = None

    return result


def extract_avaliacao_motorista_data(text: str) -> dict:
    """Extrai dados de avaliacao de motorista."""
    return _extract_recent_operational_doc_data(
        text,
        "AVALIACAO_MOTORISTA",
        extra_name_patterns=[
            r"(?:[Nn]ome|[Mm]otorist[ao]|[Cc]andidato)\s*[:\-]?\s*([A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç\s]{6,})",
            r"\b([A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ]{2,}(?:\s+[A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ]{2,}){1,6})\b",
        ],
        extra_date_patterns=[
            r"(?:[Dd]ata|[Ee]miss[ãa]o|[Aa]vali[aa][çc][ãa]o)\s*[:\-]?\s*(\d{2})/(\d{2})/(\d{1,4})",
        ],
    )


def extract_teste_pratico_data(text: str) -> dict:
    """Extrai dados de teste pratico."""
    return _extract_recent_operational_doc_data(
        text,
        "TESTE_PRATICO",
        extra_name_patterns=[
            r"(?:[Nn]ome|[Cc]andidato|[Cc]ondutor)\s*[:\-]?\s*([A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç\s]{6,})",
            r"\b([A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ]{2,}(?:\s+[A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ]{2,}){1,6})\b",
        ],
        extra_date_patterns=[
            r"(?:[Dd]ata|[Aa]vali[aa][çc][ãa]o)\s*[:\-]?\s*(\d{2})/(\d{2})/(\d{1,4})",
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
    return _extract_recent_operational_doc_data(
        text,
        "TREINAMENTO_DIRECAO_DEFENSIVA",
        extra_name_patterns=[
            r"(?:[Nn]ome|[Tt]rabalhador|[Cc]ondutor|[Mm]otorist[ao])\s*[:\-]?\s*([A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç\s]{6,})",
            r"\b([A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ]{2,}(?:\s+[A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ]{2,}){1,6})\b",
        ],
        extra_date_patterns=[
            r"(?:[Dd]ata|[Cc]ertifica[çc][ãa]o|[Ee]miss[ãa]o)\s*[:\-]?\s*(\d{2})/(\d{2})/(\d{1,4})",
        ],
    )


def extract_papeleta_controle_jornada_data(text: str) -> dict:
    """Extrai dados de papeleta de controle de jornada."""
    return _extract_recent_operational_doc_data(
        text,
        "PAPELETA_CONTROLE_JORNADA",
        extra_name_patterns=[
            r"(?:[Nn]ome|[Cc]ondutor|[Mm]otorist[ao])\s*[:\-]?\s*([A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç\s]{6,})",
            r"\b([A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ]{2,}(?:\s+[A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ]{2,}){1,6})\b",
        ],
        extra_date_patterns=[
            r"(?:[Dd]ata|[Pp]er[íi]odo|[Cc]ompet[êe]ncia)\s*[:\-]?\s*(\d{2})/(\d{2})/(\d{1,4})",
        ],
    )


def extract_treinamento_data(text: str) -> dict:
    """Extrai dados de treinamentos customizados com OCR fraco."""
    result = _extract_recent_operational_doc_data(
        text,
        "TREINAMENTO",
        extra_name_patterns=[
            r"(?:[Nn]ome|[Cc]olaborador|[Ee]mpregado|[Mm]otorist[ao])\s*[:\-]?\s*([A-Za-zÃÃ‰ÃÃ“ÃšÃ‚ÃŠÃŽÃ”Ã›ÃƒÃ•Ã‡Ã¡Ã©Ã­Ã³ÃºÃ¢ÃªÃ®Ã´Ã»Ã£ÃµÃ§\s]{6,})",
        ],
        extra_date_patterns=[
            r"(?:[Dd]ata|[Ee]miss[Ã£a]o|[Rr]ealiza[Ã§c][Ã£a]o)\s*[:\-]?\s*(\d{2})/(\d{2})/(\d{1,4})",
        ],
        allow_fallback_name=False,
        use_generic_uppercase_pattern=False,
        extra_name_patterns_only=True,
    )
    result["name"] = None
    return result


def extract_papeleta_data(text: str) -> dict:
    """Extrai dados para papeletas customizadas classificadas fora do tipo completo."""
    result = _extract_recent_operational_doc_data(
        text,
        "PAPELETA",
        extra_name_patterns=[
            r"(?:[Nn]ome|[Cc]ondutor|[Mm]otorist[ao]|[Cc]olaborador)\s*[:\-]?\s*([A-Za-zÃÃ‰ÃÃ“ÃšÃ‚ÃŠÃŽÃ”Ã›ÃƒÃ•Ã‡Ã¡Ã©Ã­Ã³ÃºÃ¢ÃªÃ®Ã´Ã»Ã£ÃµÃ§\s]{6,})",
        ],
        extra_date_patterns=[
            r"(?:[Dd]ata|[Pp]er[Ã­i]odo|[Cc]ompet[Ãªe]ncia)\s*[:\-]?\s*(\d{2})/(\d{2})/(\d{1,4})",
        ],
        allow_fallback_name=False,
        use_generic_uppercase_pattern=False,
        extra_name_patterns_only=True,
    )
    labeled_name = _extract_name_from_labeled_lines(text, "PAPELETA")
    if labeled_name:
        result["name"] = labeled_name
    elif result.get("name") and not _is_valid_name_for_doc_type(result["name"], "PAPELETA"):
        result["name"] = None
    return result


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

    # Periodo: "Fechamento: 190 21/05/2025 a 20/06/2025" -> usar APENAS data final
    period_pattern = r'[Ff]echamento\s*[:\-]\s*\d+\s+(\d{2}/\d{2}/\d{1,4})\s*a\s*(\d{2}/\d{2}/\d{1,4})'
    period_match = re.search(period_pattern, text)
    if period_match:
        # Usar APENAS a data final (group 2), não a inicial
        end = _correct_date_in_period(period_match.group(2))
        result["period"] = end
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
            else:
                generic_range = re.search(
                    r'(\d{2}/\d{2}/\d{1,4})\s*(?:a|ate|até)\s*(\d{2}/\d{2}/\d{1,4})',
                    text,
                    re.IGNORECASE,
                )
                if generic_range:
                    end = _correct_date_in_period(generic_range.group(2))
                    result["period"] = end
                else:
                    single_date = _extract_date_from_text(text)
                    if single_date:
                        result["period"] = single_date

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
        r'[Cc]olaborador(?:\(a\))?\s*[:\-]?\s*([A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç][A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç\s]{3,})',
        r'[Ee]mpregado(?:\(a\))?\s*[:\-]?\s*([A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç][A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç\s]{3,})',
        r'[Ff]uncion[áa]rio(?:\(a\))?\s*[:\-]?\s*([A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç][A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç\s]{3,})',
        r'[Cc]andidato(?:\(a\))?\s*[:\-]?\s*([A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç][A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç\s]{3,})',
        r'[Pp]aciente\s*[:\-]?\s*([A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç][A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç\s]{3,})',
        r'Eu\s*,\s*([A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç\s]{6,})\s*,\s*(?:inscrito|portador|declaro)',
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
        contextual_name = _extract_name_from_labeled_lines(text, "GEN")
        if contextual_name:
            result["name"] = contextual_name

    if not result["name"]:
        for line in text.splitlines():
            compact = re.sub(r'\s+', ' ', line).strip()
            if not compact:
                continue
            if len(compact.split()) > 6:
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
        try:
            if 1 <= int(month) <= 12:
                result["period"] = f"{month}-{year}"
                return result
        except ValueError:
            pass

    single_date = re.search(r'(\d{2})/(\d{2})/(\d{1,4})', text)
    if single_date:
        day, month, year = single_date.group(1), single_date.group(2), correct_year(single_date.group(3))
        try:
            if 1 <= int(day) <= 31 and 1 <= int(month) <= 12:
                result["period"] = f"{day}-{month}-{year}"
        except ValueError:
            pass

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


def extract_advertencia_escrita_data(text: str) -> dict:
    """Extrai dados de Advertência Escrita Disciplinar."""
    result: dict[str, str | None] = {"name": None, "period": None}

    # Nome: "COLABORADOR(a): NOME" - extrair do campo COLABORADOR, não EMPREGADOR
    name_patterns = [
        # Formato 1: "COLABORADOR(A): NOME" (com ou sem espaços)
        r'COLABORADOR\s*\(\s*A\s*\)\s*[:\-]?\s*([A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ][A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ\s]{3,})',
        # Formato 2: "COLABORADOR: NOME"
        r'COLABORADOR\s*[:\-]?\s*([A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ][A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ\s]{3,})',
        # Formato 3: "Eu, NOME, portador(a) do CPF"
        r'Eu\s*,\s*([A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç\s]{6,})\s*,\s*portador(?:a)?\s+do\s+CPF',
        # Formato 4: caixa mista/OCR
        r'[Cc]olaborador\s*(?:\(a\))?\s*[:\-]?\s*([A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç][A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç\s]{3,})',
    ]
    
    for pattern in name_patterns:
        match = re.search(pattern, text)
        if match:
            raw_name = match.group(1).strip()
            # Remover "CPF" e similares que possam estar colados
            raw_name = re.sub(r'\s*(CPF|CTPS|RG).*$', '', raw_name)
            cleaned = clean_name(raw_name)
            if cleaned and _is_valid_name_for_doc_type(cleaned, "ADVERTENCIA_ESCRITA"):
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


def detect_multiple_documents_in_pdf(text_pages: list[str], doc_type: str, logger: logging.Logger) -> list[dict]:
    """Detecta múltiplos documentos (motoristas/fechamentos) em PDF com múltiplas páginas.
    
    Retorna lista de dicts com:
    - name: nome do motorista
    - closing_number: número de fechamento (se aplicável)
    - page_indices: lista de índices de páginas que pertencem a este documento
    - period: período do documento
    """
    if doc_type != "FMM" or not text_pages:
        # Por enquanto, suporta apenas FMM com múltiplas páginas
        return []
    
    documents = []
    current_doc = None

    for page_idx, page_text in enumerate(text_pages):
        data = extract_fmm_data(page_text)
        detected_name = data.get("name")
        detected_period = data.get("period")
        closing_num = data.get("closing_number")

        if current_doc is None:
            current_doc = {
                "name": detected_name or "MOTORISTA DESCONHECIDO",
                "closing_numbers": [closing_num] if closing_num else [],
                "page_indices": [page_idx],
                "period": detected_period,
            }
            continue

        start_new_doc = False
        if detected_name and current_doc.get("name") and detected_name != current_doc.get("name"):
            start_new_doc = True
        if detected_period and current_doc.get("period") and detected_period != current_doc.get("period"):
            start_new_doc = True

        if start_new_doc:
            documents.append(current_doc)
            current_doc = {
                "name": detected_name or current_doc.get("name") or "MOTORISTA DESCONHECIDO",
                "closing_numbers": [closing_num] if closing_num else [],
                "page_indices": [page_idx],
                "period": detected_period,
            }
            continue

        current_doc["page_indices"].append(page_idx)
        if detected_name:
            current_doc["name"] = detected_name
        if closing_num and closing_num not in current_doc["closing_numbers"]:
            current_doc["closing_numbers"].append(closing_num)
        if detected_period:
            current_doc["period"] = detected_period

    if current_doc:
        documents.append(current_doc)

    logger.debug(f"Detectados {len(documents)} documentos em {len(text_pages)} páginas")
    return documents


def extract_fmm_text_by_page(
    pdf_path: Path,
    tesseract_path: str,
    poppler_path: str | None,
    logger: logging.Logger,
) -> list[str]:
    """Extrai OCR página-a-página para FMM multipágina."""
    images = pdf_to_images(pdf_path, poppler_path, dpi=OCR_DPI)
    pages_to_process = images if MAX_PAGES_TO_OCR is None else images[:MAX_PAGES_TO_OCR]

    page_texts: list[str] = []
    config_primary = build_ocr_config(psm=6)
    config_fallback = build_ocr_config(psm=3)

    for idx, image in enumerate(pages_to_process):
        processed_primary = preprocess_image_for_tables(image)
        text_primary = normalize_ocr_text(ocr_image(processed_primary, tesseract_path, config=config_primary))

        if len(text_primary.strip()) < 40:
            processed_fallback = preprocess_image_light(image)
            text_fallback = normalize_ocr_text(ocr_image(processed_fallback, tesseract_path, config=config_fallback))
            chosen = text_fallback if len(text_fallback) > len(text_primary) else text_primary
        else:
            chosen = text_primary

        page_texts.append(chosen)
        logger.debug(f"  FMM split OCR página {idx + 1}: {len(chosen)} chars")

    return page_texts


def split_fmm_pdf_by_period_and_driver(
    pdf_path: Path,
    scanner_dir: Path,
    docs: list[dict],
    logger: logging.Logger,
) -> list[Path]:
    """Gera PDFs separados para FMM multipágina por motorista/período."""
    if not docs or len(docs) <= 1:
        return []

    if PdfReader is None or PdfWriter is None:
        logger.warning("Split FMM multipágina indisponível: instale 'pypdf'.")
        return []

    reader = PdfReader(str(pdf_path))
    created_paths: list[Path] = []
    expected_outputs = 0
    had_generation_error = False

    for index, doc in enumerate(docs, start=1):
        page_indices = doc.get("page_indices") or []
        valid_indices = [i for i in page_indices if isinstance(i, int) and 0 <= i < len(reader.pages)]
        if not valid_indices:
            logger.warning(f"  Split FMM bloco {index} sem páginas válidas - ignorado")
            continue

        expected_outputs += 1

        try:
            writer = PdfWriter()
            for page_idx in valid_indices:
                writer.add_page(reader.pages[page_idx])

            extracted_name = doc.get("name") or "NOME NAO LOCALIZADO"
            extracted_period = doc.get("period") or "SEM PERIODO"
            closing_number = doc.get("closing_number")
            split_filename = build_new_filename("FMM", extracted_name, extracted_period, closing_number=closing_number)
            split_target = resolve_filename_conflict(scanner_dir / split_filename)

            with split_target.open("wb") as fh:
                writer.write(fh)

            created_paths.append(split_target)
            logger.info(
                f"  FMM split gerado ({index}/{len(docs)}): {split_target.name} "
                f"[{len(valid_indices)} página(s)]"
            )
        except Exception as e:
            had_generation_error = True
            logger.warning(f"  Falha ao gerar bloco FMM {index}: {e}")

    if expected_outputs > 0 and not had_generation_error and len(created_paths) == expected_outputs:
        pdf_path.unlink(missing_ok=True)
        logger.info(f"  PDF original removido após split: {pdf_path.name}")
    elif created_paths:
        logger.warning(
            "  Split FMM parcial detectado: PDF original mantido para evitar perda de dados."
        )

    return created_paths

def aggregate_multipage_closure(documents: list[dict]) -> list[dict]:
    """Agrupa números de fechamento para motoristas que aparecem múltiplas vezes.
    
    Transforma:
    - closing_numbers: [190, 191, 192] → período com todos os nros
    - Mantém separado se motoristas diferentes
    """
    if len(documents) == 1:
        # Apenas 1 motorista
        doc = documents[0]
        closing_str = "-".join(doc["closing_numbers"]) if doc.get("closing_numbers") else None
        return [{
            "name": doc["name"],
            "closing_number": closing_str,
            "period": doc["period"],
            "page_indices": doc["page_indices"],
        }]
    
    # Múltiplos motoristas - retorna cada um separado
    result = []
    for doc in documents:
        closing_str = "-".join(doc["closing_numbers"]) if doc.get("closing_numbers") else None
        result.append({
            "name": doc["name"],
            "closing_number": closing_str,
            "period": doc["period"],
            "page_indices": doc["page_indices"],
        })
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

    if result.get("name") and not _is_valid_name_for_doc_type(result["name"], "NF"):
        result["name"] = None

    return result


def extract_recibo_data(text: str) -> dict:
    """Extrai dados de Recibo com heuristica administrativa."""
    result = _extract_named_doc_data(
        text,
        "RECIBO",
        name_patterns=[
            r"(?:Recebi(?:mos)?\s+de|Pagador|Recebedor|Favorecido|Cliente|Nome)\s*[:\-]?\s*([A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç\s]{6,})",
            r"Eu\s*,\s*([A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç\s]{6,})\s*,",
        ],
        date_patterns=[
            r"(\d{2})/(\d{2})/(\d{1,4})",
        ],
        fallback_name=None,
    )
    if not result.get("name"):
        fallback = extract_fallback_data(text)
        if fallback.get("name"):
            result["name"] = fallback.get("name")
        if fallback.get("period") and not result.get("period"):
            result["period"] = fallback.get("period")
    if result.get("name") and not _is_valid_name_for_doc_type(result["name"], "RECIBO"):
        result["name"] = None
    return result


def extract_declaracao_data(text: str) -> dict:
    """Extrai dados de Declaracao com heuristica administrativa."""
    result = _extract_named_doc_data(
        text,
        "DECLARACAO",
        name_patterns=[
            r"(?:Declarante|Declarant[ea]|Nome)\s*[:\-]?\s*([A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç\s]{6,})",
            r"Eu\s*,\s*([A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç\s]{6,})\s*,",
        ],
        date_patterns=[
            r"(\d{2})/(\d{2})/(\d{1,4})",
        ],
        fallback_name=None,
    )
    if not result.get("name"):
        fallback = extract_fallback_data(text)
        if fallback.get("name"):
            result["name"] = fallback.get("name")
        if fallback.get("period") and not result.get("period"):
            result["period"] = fallback.get("period")
    if result.get("name") and not _is_valid_name_for_doc_type(result["name"], "DECLARACAO"):
        result["name"] = None
    return result


def extract_contrato_data(text: str) -> dict:
    """Extrai dados de Contrato com heuristica administrativa."""
    result = _extract_named_doc_data(
        text,
        "CONTRATO",
        name_patterns=[
            r"EMPREGADO\s*\(\s*A\s*\)\s*[:\-\.]?\s*([^\n\r]{6,80})",
            r"CONTRATADO\s*\(\s*A\s*\)\s*[:\-\.]?\s*([^\n\r]{6,80})",
            r"(?:Contratado\s*\(\s*a\s*\)|Contratado|Empregado\s*\(\s*a\s*\)|Empregado)\s*[:\-\.]?\s*([^\n\r]{6,80})",
        ],
        date_patterns=[
            r"(\d{2})/(\d{2})/(\d{1,4})",
        ],
        fallback_name=None,
    )
    if not result.get("name"):
        fallback = extract_fallback_data(text)
        if fallback.get("name"):
            result["name"] = fallback.get("name")
        if fallback.get("period") and not result.get("period"):
            result["period"] = fallback.get("period")
    if result.get("name"):
        result["name"] = re.sub(r"^(?:EMPREGADO|CONTRATADO)\s*\(\s*A\s*\)\s*", "", result["name"], flags=re.IGNORECASE).strip()
        name_upper = result["name"].upper()
        if any(token in name_upper for token in ("LTDA", "CNPJ", "TRANSPORTADORA BRASIL CENTRAL")):
            result["name"] = None
    if result.get("name") and not _is_valid_name_for_doc_type(result["name"], "CONTRATO"):
        result["name"] = None
    return result


def extract_alteracao_beneficiarios_data(text: str) -> dict:
    """Extrai dados de Alteracao/Indicacao de Beneficiarios (ex.: Icatu)."""
    result = _extract_named_doc_data(
        text,
        "ALTERACAO_BENEFICIARIOS",
        name_patterns=[
            r"(?:Titular|Segurado|Proponente|Nome)\s*[:\-]?\s*([A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç\s]{6,})",
        ],
        date_patterns=[
            r"(\d{2})/(\d{2})/(\d{1,4})",
        ],
        fallback_name=None,
    )

    if result.get("name") and not _is_valid_name_for_doc_type(result["name"], "ALTERACAO_BENEFICIARIOS"):
        result["name"] = None

    return result


def extract_dut_declaracao_data(text: str) -> dict:
    """Extrai dados para declaracoes envolvendo DUT."""
    result = _extract_named_doc_data(
        text,
        "DUT_DECLARACAO",
        name_patterns=[
            r"(?:Declarante|Nome|Propriet[áa]rio|Comprador|Vendedor)\s*[:\-]?\s*([A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç\s]{6,})",
            r"DUT\s+([A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç\s]{6,})",
        ],
        date_patterns=[
            r"(\d{2})/(\d{2})/(\d{1,4})",
        ],
        fallback_name=None,
    )
    if not result.get("name"):
        fallback = extract_fallback_data(text)
        if fallback.get("name"):
            result["name"] = fallback.get("name")
    if result.get("name") and not _is_valid_name_for_doc_type(result["name"], "DUT_DECLARACAO"):
        result["name"] = None
    return result


def extract_politica_violacoes_velocidade_data(text: str) -> dict:
    """Extrai dados para política de violações de velocidade."""
    result = _extract_named_doc_data(
        text,
        "POLITICA_VIOLACOES_VELOCIDADE",
        name_patterns=[
            r"(?:Colaborador|Empregado|Condutor|Motorista|Nome)\s*[:\-]?\s*([A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç\s]{6,})",
        ],
        date_patterns=[
            r"(\d{2})/(\d{2})/(\d{1,4})",
            r"(\d{2})/(\d{1,4})",
        ],
        fallback_name=None,
    )
    if result.get("name") and not _is_valid_name_for_doc_type(result["name"], "POLITICA_VIOLACOES_VELOCIDADE"):
        result["name"] = None
    return result


def extract_comprovante_data(text: str) -> dict:
    """Extrai dados de Comprovante com heuristica administrativa."""
    result = _extract_named_doc_data(
        text,
        "COMPROVANTE",
        name_patterns=[
            r"(?:Favorecido|Benefici.?rio|Cliente|Nome)\s*[:\-]?\s*([A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç\s]{6,})",
            r"(?:Titular\s+da\s+conta|Titular)\s*[:\-]?\s*([A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç\s]{6,})",
        ],
        date_patterns=[
            r"(\d{2})/(\d{2})/(\d{1,4})",
        ],
        fallback_name=None,
    )
    if not result.get("name"):
        fallback = extract_fallback_data(text)
        if fallback.get("name"):
            result["name"] = fallback.get("name")
        if fallback.get("period") and not result.get("period"):
            result["period"] = fallback.get("period")
    if result.get("name") and not _is_valid_name_for_doc_type(result["name"], "COMPROVANTE"):
        result["name"] = None
    return result


def extract_relatorio_abastecimento_data(text: str) -> dict:
    """Extrai dados de relatórios operacionais de abastecimento/reembolso."""
    result: dict[str, str | None] = {"name": None, "period": None}

    motorista_match = re.search(
        r"[Mm]otorista\s*[:\-]?\s*(?:\d{3,6}\s+)?([A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç\s]{5,})",
        text,
    )
    if motorista_match:
        candidate = clean_name(motorista_match.group(1))
        if candidate:
            result["name"] = candidate

    if not result.get("name"):
        motorista_alt = re.search(
            r"(?:[Cc]ondutor|[Mm]otorista)\s*[:\-]?\s*\n?\s*(?:\d{3,6}\s*)?([A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç\s]{5,})",
            text,
        )
        if motorista_alt:
            candidate = clean_name(motorista_alt.group(1))
            if candidate and _is_valid_name_for_doc_type(candidate, "RELATORIO_ABASTECIMENTO"):
                result["name"] = candidate

    if not result.get("name"):
        fallback = extract_fallback_data(text)
        if fallback.get("name"):
            result["name"] = fallback.get("name")

    if result.get("name") and not _is_valid_name_for_doc_type(result["name"], "RELATORIO_ABASTECIMENTO"):
        result["name"] = None

    range_match = re.search(r"(\d{2}/\d{2}/\d{1,4})\s*a\s*(\d{2}/\d{2}/\d{1,4})", text)
    if range_match:
        # Usar APENAS a data final (group 2), não a inicial
        end = _correct_date_in_period(range_match.group(2).replace("/", "-"))
        result["period"] = end
    else:
        single_date = _extract_date_from_text(text)
        if single_date:
            result["period"] = single_date

    return result


def extract_solicitacao_contratacao_data(text: str) -> dict:
    """Extrai dados de e-mails de solicitação/autorização de contratação."""
    result = _extract_named_doc_data(
        text,
        "SOLICITACAO_CONTRATACAO",
        name_patterns=[
            r"(?:Candidato|Colaborador|Empregado|Nome)\s*[:\-]?\s*([A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç\s]{6,})",
            r"Eu\s*,\s*([A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç\s]{6,})\s*,",
        ],
        date_patterns=[
            r"(\d{2})/(\d{2})/(\d{1,4})",
        ],
        fallback_name=None,
    )

    if not result.get("name"):
        fallback = extract_fallback_data(text)
        if fallback.get("name"):
            result["name"] = fallback.get("name")
        if fallback.get("period") and not result.get("period"):
            result["period"] = fallback.get("period")

    if result.get("name") and not _is_valid_name_for_doc_type(result["name"], "SOLICITACAO_CONTRATACAO"):
        result["name"] = None

    if not result.get("period"):
        date = _extract_date_from_text(text)
        if date:
            result["period"] = date

    return result


def extract_abertura_vaga_data(text: str) -> dict:
    """Extrai dados de documentos de abertura/requisição de vaga."""
    result = _extract_named_doc_data(
        text,
        "ABERTURA_VAGA",
        name_patterns=[
            r"(?:Candidato|Nome\s+do\s+Candidato|Colaborador|Empregado)\s*[:\-]?\s*([A-Za-zÁÉÍÓÚÂÊÎÔÛÃÕÇáéíóúâêîôûãõç\s]{6,})",
        ],
        date_patterns=[
            r"(\d{2})/(\d{2})/(\d{1,4})",
            r"(\d{2})-(\d{2})-(\d{1,4})",
        ],
        fallback_name=None,
    )

    if not result.get("name"):
        fallback = extract_fallback_data(text)
        if fallback.get("name"):
            result["name"] = fallback.get("name")
        if fallback.get("period") and not result.get("period"):
            result["period"] = fallback.get("period")

    if result.get("name") and not _is_valid_name_for_doc_type(result["name"], "ABERTURA_VAGA"):
        result["name"] = None

    return result


EXTRACTORS = {
    "FMM": extract_fmm_data,
    "CP": extract_cp_data,
    "FN": extract_fn_data,
    "MBV": extract_mbv_data,
    "AP": extract_ap_data,
    "ADVERTENCIA_ESCRITA": extract_advertencia_escrita_data,
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
    "TREINAMENTO": extract_treinamento_data,
    "PAPELETA_CONTROLE_JORNADA": extract_papeleta_controle_jornada_data,
    "PAPELETA": extract_papeleta_data,
    "QUESTIONARIO_ACOLHIMENTO": extract_questionario_acolhimento_data,
    "DECLARACAO_RACIAL": extract_declaracao_racial_data,
    "NF": extract_nf_data,
    "RECIBO": extract_recibo_data,
    "DECLARACAO": extract_declaracao_data,
    "CONTRATO": extract_contrato_data,
    "COMPROVANTE": extract_comprovante_data,
    "RELATORIO_ABASTECIMENTO": extract_relatorio_abastecimento_data,
    "SOLICITACAO_CONTRATACAO": extract_solicitacao_contratacao_data,
    "ABERTURA_VAGA": extract_abertura_vaga_data,
    "ALTERACAO_BENEFICIARIOS": extract_alteracao_beneficiarios_data,
    "DUT_DECLARACAO": extract_dut_declaracao_data,
    "POLITICA_VIOLACOES_VELOCIDADE": extract_politica_violacoes_velocidade_data,
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
    "ADVERTENCIA_ESCRITA": "ADVERTENCIA ESCRITA",
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
    "TREINAMENTO": "TREINAMENTO",
    "PAPELETA_CONTROLE_JORNADA": "PAPELETA CONTROLE JORNADA",
    "PAPELETA": "PAPELETA",
    "QUESTIONARIO_ACOLHIMENTO": "QUESTIONARIO ACOLHIMENTO",
    "DECLARACAO_RACIAL": "DECLARACAO RACIAL",
    "NF": "NOTA FISCAL",
    "RECIBO": "RECIBO",
    "DECLARACAO": "DECLARACAO",
    "CONTRATO": "CONTRATO",
    "COMPROVANTE": "COMPROVANTE",
    "RELATORIO_ABASTECIMENTO": "RELATORIO ABASTECIMENTO",
    "SOLICITACAO_CONTRATACAO": "SOLICITACAO CONTRATACAO",
    "ABERTURA_VAGA": "ABERTURA VAGA",
    "ALTERACAO_BENEFICIARIOS": "ALTERACAO BENEFICIARIOS",
    "DUT_DECLARACAO": "DUT DECLARACAO",
    "POLITICA_VIOLACOES_VELOCIDADE": "POLITICA VIOLACOES VELOCIDADE",
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
    """Normaliza competencia para uma data unica no formato DD-MM-YYYY ou MM-YYYY.

    Regras:
    - Se houver data completa no texto, usa a primeira encontrada.
    - Se houver apenas MM-YYYY, preserva o mes/ano sem inventar dia.
    - Se ausente, retorna SEM DATA.
    """
    if not period:
        return "SEM DATA"

    text = period.strip()
    if not text:
        return "SEM DATA"

    # Intervalo de datas: usa a data final quando disponível
    date_range = re.search(
        r"(\d{1,2}[/-]\d{1,2}[/-]\d{1,4})\s*(?:a|ate|até)\s*(\d{1,2}[/-]\d{1,2}[/-]\d{1,4})",
        text,
        re.IGNORECASE,
    )
    if date_range:
        end = _correct_date_in_period(date_range.group(2))
        if end != "SEM DATA":
            return end

    full_date = re.search(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{1,4})\b", text)
    if full_date:
        normalized = _normalize_date_parts(full_date.group(1), full_date.group(2), full_date.group(3))
        if normalized:
            return normalized

    month_year = re.search(r"\b(\d{1,2})[/-](\d{2,4})(?![/-]\d)\b", text)
    if month_year:
        month = _ocr_digits(month_year.group(1))
        year = _ocr_digits(month_year.group(2))
        if month and year:
            month_int = int(month)
            if 1 <= month_int <= 12:
                return f"{month_int:02d}-{correct_year(year)}"

    extracted = _extract_date_from_text(text)
    if extracted:
        return extracted

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
    """Mantém compatibilidade sem mover arquivo (política: apenas renomear)."""
    _ = scanner_dir
    logger.warning(f"  Quarentena desabilitada: arquivo mantido no diretório original ({pdf_path.name})")
    return pdf_path


def move_to_review_queue(pdf_path: Path, scanner_dir: Path, logger: logging.Logger) -> Path | None:
    """Mantém compatibilidade sem mover arquivo (política: apenas renomear)."""
    _ = scanner_dir
    logger.warning(f"  Revisão sem movimentação: arquivo mantido no diretório original ({pdf_path.name})")
    return pdf_path


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
    
    # RECOMENDAÇÃO 1: Inicializar monitor (se disponível)
    confidence_monitor = None
    if CONFIDENCE_MONITOR_AVAILABLE:
        try:
            confidence_monitor = ConfidenceMonitor()
        except Exception as exc:
            logger.warning(f"Confidence monitor indisponivel para este processamento: {exc}")

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
        
        # RECOMENDAÇÃO 1: Registrar classificações com confiança baixa (< 80%)
        if confidence_monitor and result.confidence_score < LOW_CONFIDENCE_THRESHOLD:
            try:
                confidence_monitor.log_low_confidence(
                    filename=pdf_path.name,
                    doc_type=doc_type,
                    confidence_score=result.confidence_score,
                    extracted_name=None,  # Será preenchido depois
                    extracted_period=None,  # Será preenchido depois
                    ocr_preview=full_text,
                    logger=logger,
                )
            except Exception as e:
                logger.debug(f"Erro ao registrar Low-Confidence: {e}")

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
                    result.extracted_name = "NOME NAO LOCALIZADO"

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

                    # FMM multipágina: separar em PDFs por motorista/período quando detectar mudança.
                    try:
                        fmm_pages_text = extract_fmm_text_by_page(pdf_path, tesseract_path, poppler_path, logger)
                        raw_docs = detect_multiple_documents_in_pdf(fmm_pages_text, "FMM", logger)
                        docs = aggregate_multipage_closure(raw_docs)
                        split_paths = split_fmm_pdf_by_period_and_driver(pdf_path, scanner_dir, docs, logger)

                        if split_paths:
                            result.new_path = split_paths[0]
                            result.status = ProcessStatus.RENAMED
                            logger.info(f"  FMM multipágina dividido em {len(split_paths)} arquivo(s)")
                            return result
                    except Exception as split_err:
                        logger.warning(f"  Falha no split FMM multipágina: {split_err}")

            # Validação final transversal de nome (edge cases para todos os tipos).
            if result.extracted_name and result.extracted_name != "NOME NAO LOCALIZADO":
                validation_type = result.doc_type if result.doc_type else "GEN"
                if not _is_valid_name_for_doc_type(result.extracted_name, validation_type):
                    logger.warning(
                        f"  Nome rejeitado por validacao ({validation_type}): {result.extracted_name}"
                    )
                    if validation_type == "MBV":
                        result.extracted_name = "NOME NAO LOCALIZADO"
                    else:
                        result.status = ProcessStatus.UNIDENTIFIED
                        return result

            if not result.extracted_name:
                # Fallback: se tipo foi identificado, renomear com tipo de documento no lugar do nome
                if result.doc_type and result.doc_type != "GEN":
                    filename_name = _extract_name_from_filename(pdf_path, result.doc_type)
                    if filename_name:
                        logger.info(f"  Nome via filename fallback: {filename_name}")
                        result.extracted_name = filename_name
                    else:
                        logger.warning(f"  Tipo={doc_type} mas nome nao encontrado - usando NOME NAO LOCALIZADO")
                        result.extracted_name = "NOME NAO LOCALIZADO"
                    # Garantir que temos período para o arquivo
                    if not result.extracted_period:
                        result.extracted_period = "SEM DATA"
                else:
                    # Tipo não foi identificado (GEN) e nome também não
                    logger.warning(f"  Tipo={doc_type} e nome nao encontrado - arquivo mantido sem alteracao")
                    result.status = ProcessStatus.UNIDENTIFIED
                    return result

            if not result.extracted_period and doc_type not in {"MBV"}:
                logger.warning(f"  Tipo={doc_type} nome={result.extracted_name} mas periodo nao encontrado")
                result.extracted_period = "SEM PERIODO"

            if _is_suspicious_period(result.extracted_period, result.doc_type, result.confidence_score):
                logger.warning(
                    f"  Data suspeita para {result.doc_type} em OCR de baixa confianca: {result.extracted_period}. "
                    "Substituindo por SEM DATA."
                )
                result.extracted_period = "SEM DATA"

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
            poor_name_signal = result.extracted_name in {None, "NOME NAO LOCALIZADO", "REVISAR NOME"}
            poor_period_signal = result.extracted_period in {None, "SEM DATA", "SEM PERIODO"}
            if result.confidence_score < max(50.0, min_required - 15.0) and (poor_name_signal or poor_period_signal):
                logger.warning("  Confianca muito baixa com campos fracos: mantendo arquivo sem renomeacao para REVISAO.")
                result.status = ProcessStatus.REVIEW
                return result

            logger.warning("  Gate de confianca ativo: mantendo arquivo para RENOMEACAO sem mover para _REVISAO.")

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
        defer_on_transient=True,
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
    confidence_gate_enabled = config.get("confidence_gate_enabled", True)

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
    log_optional_runtime_warnings(logger)

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
