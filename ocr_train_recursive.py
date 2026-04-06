#!/usr/bin/env python3
"""
Treino OCR recursivo (inclui subpastas) para aprender modelos adicionais.

Objetivo:
- Ler PDFs em todas as subpastas da pasta de scanner.
- Manter o processamento principal inalterado (main.py continua lendo somente raiz).
- Detectar tipos/modelos novos e gerar assinaturas customizadas.
- Salvar os modelos em models/custom_models.json para carregamento automatico.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable

from main import (
    PROJECT_ROOT,
    DOC_TYPE_SIGNATURES,
    DOC_TYPE_TITLE_HINTS,
    classify_document,
    find_tesseract_path,
    find_poppler_path,
    configure_tesseract_command,
    pdf_to_images,
    preprocess_image,
    ocr_image,
    build_ocr_config,
    normalize_ocr_text,
    load_config,
    setup_logging,
    CUSTOM_MODELS_FILE,
    _text_has_title_hint,
)

STANDARD_DOC_TYPES = {
    "FMM",
    "CP",
    "FN",
    "MBV",
    "AP",
    "ASO_ADMISSIONAL",
    "ASO_DEMISSIONAL",
    "ATESTADO_MEDICO",
    "CTPS",
    "CNH",
    "CURRICULO",
    "FGTS",
    "HOLERITE",
    "PPP",
    "AVALIACAO_MOTORISTA",
    "TESTE_PRATICO",
    "TESTE_CONHECIMENTOS_GERAIS",
    "TREINAMENTO_DIRECAO_DEFENSIVA",
    "PAPELETA_CONTROLE_JORNADA",
    "QUESTIONARIO_ACOLHIMENTO",
    "DECLARACAO_RACIAL",
    "NF",
    "RECIBO",
    "DECLARACAO",
    "CONTRATO",
    "COMPROVANTE",
    "GEN",
}

MIN_FILES_FOR_NEW_MODEL = 2

CONTENT_TYPE_HINTS = {
    "ASO_ADMISSIONAL": [r"\bASO\b", r"ADMISSIONA(?:L|IS)", r"EXAME\s+ADMISSIONA(?:L|IS)", r"SAUDE\s+OCUPACIONAL"],
    "ASO_DEMISSIONAL": [r"\bASO\b", r"DEMISSIONA(?:L|IS)", r"EXAME\s+DEMISSIONA(?:L|IS)", r"DEMISS[ÃA]O"],
    "ATESTADO_MEDICO": [r"ATESTADO", r"M[ÉE]DIC[OA]", r"AFASTAMENTO"],
    "CTPS": [r"\bCTPS\b", r"CARTEIRA\s+DE\s+TRABALHO", r"CARTEIRA\s+PROFISSIONAL"],
    "CNH": [r"\bCNH\b", r"CARTEIRA\s+NACIONAL\s+DE\s+HABILITA[ÇC][ÃA]O", r"HABILITA[ÇC][ÃA]O"],
    "CURRICULO": [r"CURR[ÍI]CULO", r"CURRICULUM", r"VITAE"],
    "FGTS": [r"\bFGTS\b", r"FUNDO\s+DE\s+GARANTIA", r"GUIA\s+DO\s+FGTS"],
    "HOLERITE": [r"HOLERITE", r"CONTRACHEQUE", r"RECIBO\s+DE\s+PAGAMENTO"],
    "PPP": [r"\bPPP\b", r"PERFIL\s+PROFISSIOGRAFICO", r"PREVIDENCIARIO"],
    "AVALIACAO_MOTORISTA": [r"AVALIACAO\s+DE?\s+MOTORISTA", r"MOTORISTA", r"AVALIACAO"],
    "TESTE_PRATICO": [r"TESTE\s+PRATICO", r"PRATICO", r"CONDUTOR"],
    "TESTE_CONHECIMENTOS_GERAIS": [r"TESTE\s+DE\s+CONHECIMENTOS\s+GERAIS", r"CONHECIMENTOS\s+GERAIS", r"TESTE"],
    "TREINAMENTO_DIRECAO_DEFENSIVA": [r"TREINAMENTO\s+DE\s+DIRECAO\s+DEFENSIVA", r"DIRECAO\s+DEFENSIVA", r"TREINAMENTO"],
    "PAPELETA_CONTROLE_JORNADA": [r"PAPELETA\s+CONTROLE\s+DE\s+JORNADA", r"CONTROLE\s+DE\s+JORNADA", r"JORNADA"],
    "QUESTIONARIO_ACOLHIMENTO": [r"QUESTIONARIO\s+DE\s+ACOLHIMENTO", r"ACOLHIMENTO", r"QUESTIONARIO"],
    "DECLARACAO_RACIAL": [r"DECLARACAO\s+RACIAL", r"AUTODECLARACAO\s+RACIAL", r"DECLARACAO"],
}

TITLE_HINT_WORDS = {
    "ABERTURA", "ACORDO", "ADVERTENCIA", "ATESTADO", "AUTODECLARACAO", "AUTORIZACAO",
    "AVALIACAO", "AVISO", "BOLETO", "CARTA", "CARTAO", "CARTEIRA", "CERTIDAO",
    "CERTIFICADO", "CHECKLIST", "CHECK", "COMODATO", "COMUNICADO", "CONTRATO",
    "CURRICULO", "DECLARACAO", "DEMONSTRATIVO", "DESISTENCIA", "DOCUMENTO", "DUT",
    "EPI", "EXAME", "ESPELHO", "FGTS", "FICHA", "FOLHA", "FORMULARIO", "GUIA",
    "HISTORICO", "HOLERITE", "INFORMACAO", "INTEGRACAO", "LISTA", "MANUAL", "MULTA",
    "MOTORISTA", "AVALIACAO", "PRATICO", "CONHECIMENTOS", "TREINAMENTO", "DIRECAO",
    "DEFENSIVA", "PAPELETA", "QUESTIONARIO", "ACOLHIMENTO", "RACIAL",
    "NORMAS", "NOTA", "NOTIFICACAO", "ORDEM", "PPP", "PROCESSO",
    "PROGRAMA", "QUALIFICACAO", "RECIBO", "RELATORIO", "REQUISICAO", "SOLICITACAO",
    "TERMO", "VACINA", "VAGA", "PONTO", "REGISTRO", "PRESENCA", "PAGAMENTO",
    "PESSOAL", "SOCIAL", "TRABALHO", "SEGURANCA", "OCUPACIONAL", "MEDICO",
}

STOPWORDS = {
    "DE",
    "DA",
    "DO",
    "DAS",
    "DOS",
    "PARA",
    "COM",
    "SEM",
    "POR",
    "QUE",
    "UMA",
    "UM",
    "EM",
    "NO",
    "NA",
    "NOS",
    "NAS",
    "E",
}


def normalize_doc_type(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_]+", "_", value.strip().upper())
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    return normalized


def collect_recursive_pdfs(base_dir: Path) -> list[Path]:
    return sorted(
        [p for p in base_dir.rglob("*.pdf") if p.is_file()],
        key=lambda item: str(item).lower(),
    )


def fast_ocr_for_training(pdf_path: Path, tesseract_path: str, poppler_path: str | None) -> str:
    """OCR rapido para treino: primeira pagina, DPI 300, PSM 6."""
    images = pdf_to_images(pdf_path, poppler_path, dpi=300)
    if not images:
        return ""
    first_page = preprocess_image(images[0])
    config = build_ocr_config(psm=6)
    text = ocr_image(first_page, tesseract_path, config=config)
    return normalize_ocr_text(text)


def extract_keywords(texts: Iterable[str]) -> Counter:
    words = Counter()
    for text in texts:
        for token in re.findall(r"[A-ZÁÉÍÓÚÂÊÔÃÕÇ]{4,}", text.upper()):
            if token in STOPWORDS:
                continue
            words[token] += 1
    return words


def regex_from_label(label: str) -> str:
    escaped = re.escape(label.replace("_", " "))
    escaped = escaped.replace(r"\ ", r"\s+")
    return escaped


def propose_signature(doc_type: str, texts: list[str]) -> dict[str, list[str]] | None:
    if not texts:
        return None

    keyword_counter = extract_keywords(texts)
    if not keyword_counter:
        required = [regex_from_label(doc_type)]
        return {"required": required, "optional": []}

    min_support = max(1, math.ceil(len(texts) * 0.4))
    filtered_keywords = [word for word, count in keyword_counter.items() if count >= min_support]

    required = [regex_from_label(doc_type)]
    for word in filtered_keywords[:2]:
        pattern = re.escape(word)
        if pattern not in required:
            required.append(pattern)

    optional = [re.escape(word) for word in filtered_keywords[2:8]]
    return {
        "required": required,
        "optional": optional,
    }


def discover_candidate_doc_type(text: str) -> str | None:
    """Tenta inferir o tipo do documento pelo conteudo OCR."""
    doc_type = classify_document(text)
    if doc_type:
        return doc_type

    upper_text = text.upper()
    for candidate, hints in CONTENT_TYPE_HINTS.items():
        if all(re.search(pattern, upper_text, re.IGNORECASE) for pattern in hints[:1]):
            return candidate
        hit_count = sum(1 for pattern in hints if re.search(pattern, upper_text, re.IGNORECASE))
        if hit_count >= 2:
            return candidate

    for candidate, phrases in DOC_TYPE_TITLE_HINTS.items():
        if _text_has_title_hint(text, phrases):
            return candidate

    # Fallback mais geral: usa o primeiro cabeçalho OCR que pareça titulo documental.
    for line in text.splitlines()[:20]:
        compact = re.sub(r"\s+", " ", line).strip()
        if not compact or len(compact) < 4:
            continue

        upper_line = compact.upper()
        if re.search(r"\d", upper_line):
            continue

        words = re.findall(r"[A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ]+", upper_line)
        if not words or len(words) > 8:
            continue

        if not any(word in TITLE_HINT_WORDS for word in words):
            continue

        candidate = normalize_doc_type(upper_line)
        if candidate and candidate not in STANDARD_DOC_TYPES and len(candidate) >= 3:
            return candidate

    return None


def merge_custom_models(custom_models_path: Path, learned_signatures: dict, learned_corrections: dict) -> dict:
    existing = {
        "doc_type_signatures": {},
        "ocr_corrections": {},
    }

    if custom_models_path.is_file():
        try:
            existing = json.loads(custom_models_path.read_text(encoding="utf-8"))
        except Exception:
            existing = {
                "doc_type_signatures": {},
                "ocr_corrections": {},
            }

    existing.setdefault("doc_type_signatures", {})
    existing.setdefault("ocr_corrections", {})

    for doc_type, signature in learned_signatures.items():
        target = existing["doc_type_signatures"].setdefault(doc_type, {"required": [], "optional": []})

        for key in ("required", "optional"):
            target.setdefault(key, [])
            known = set(target[key])
            for pattern in signature.get(key, []):
                if pattern not in known:
                    target[key].append(pattern)
                    known.add(pattern)

    for wrong, correct in learned_corrections.items():
        existing["ocr_corrections"].setdefault(wrong, correct)

    custom_models_path.parent.mkdir(parents=True, exist_ok=True)
    custom_models_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    return existing


def train_recursive(input_dir: Path, apply_changes: bool) -> dict:
    logger = setup_logging()
    tesseract_path = find_tesseract_path()
    poppler_path = find_poppler_path()
    configure_tesseract_command(tesseract_path)

    pdf_files = collect_recursive_pdfs(input_dir)
    logger.info(f"Treino recursivo: {len(pdf_files)} PDFs encontrados em {input_dir}")
    if not pdf_files:
        return {
            "total_files": 0,
            "processed": 0,
            "new_doc_types": [],
            "custom_models_file": str(CUSTOM_MODELS_FILE),
        }

    known_doc_types = set(DOC_TYPE_SIGNATURES.keys())
    texts_by_expected_type: dict[str, list[str]] = defaultdict(list)
    learned_corrections: dict[str, str] = {}
    processed = 0
    errors = {}
    unclassified_files: list[str] = []

    for pdf_path in pdf_files:
        try:
            normalized = fast_ocr_for_training(pdf_path, tesseract_path, poppler_path)
            candidate_type = discover_candidate_doc_type(normalized)
            if candidate_type:
                texts_by_expected_type[candidate_type].append(normalized)
            else:
                unclassified_files.append(str(pdf_path))
            processed += 1
            if processed % 25 == 0:
                logger.info(f"Treino recursivo: {processed}/{len(pdf_files)} PDFs analisados")
        except Exception as exc:
            errors[str(pdf_path)] = str(exc)

    learned_signatures: dict[str, dict[str, list[str]]] = {}
    discovered_new_types: list[str] = []

    for doc_type, texts in texts_by_expected_type.items():
        if not texts:
            continue

        if doc_type not in known_doc_types:
            if len(texts) < MIN_FILES_FOR_NEW_MODEL:
                continue
            signature = propose_signature(doc_type, texts)
            if signature:
                learned_signatures[doc_type] = signature
                discovered_new_types.append(doc_type)

    applied = False
    if apply_changes and (learned_signatures or learned_corrections):
        merge_custom_models(CUSTOM_MODELS_FILE, learned_signatures, learned_corrections)
        applied = True

    report = {
        "total_files": len(pdf_files),
        "processed": processed,
        "unclassified_files_count": len(unclassified_files),
        "unclassified_files_sample": unclassified_files[:20],
        "errors": errors,
        "new_doc_types": sorted(discovered_new_types),
        "new_doc_types_count": len(discovered_new_types),
        "learned_signatures": learned_signatures,
        "apply_changes": apply_changes,
        "applied": applied,
        "custom_models_file": str(CUSTOM_MODELS_FILE),
        "standard_doc_types": sorted(STANDARD_DOC_TYPES),
    }

    report_file = PROJECT_ROOT / "ocr_training_recursive_report.json"
    report_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"Relatorio de treino salvo em: {report_file}")
    if applied:
        logger.info(f"Modelos customizados atualizados em: {CUSTOM_MODELS_FILE}")

    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Treino OCR recursivo em subpastas")
    parser.add_argument(
        "--input-dir",
        type=str,
        default=None,
        help="Diretorio base para treino recursivo. Default: scanner_dir do config.ini",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Nao grava modelos; apenas gera relatorio.",
    )
    args = parser.parse_args()

    config = load_config()
    base_dir = Path(args.input_dir) if args.input_dir else Path(config["scanner_dir"])

    if not base_dir.is_dir():
        raise FileNotFoundError(f"Diretorio de treino nao encontrado: {base_dir}")

    report = train_recursive(base_dir, apply_changes=not args.dry_run)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
