#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
Test Edge Cases with Aggressive OCR Preprocessing
Recomendação 2: Verificar manualmente 3 arquivos não classificados com OCR agressivo.

Uso:
  python test_aggressive_ocr.py --unclassified-only  # Testa apenas não classificados
  python test_aggressive_ocr.py --file "PATH\TO\PDF" # Testa PDF específico
  python test_aggressive_ocr.py --dir "PATH\TO\DIR"  # Testa arquivos em diretório
"""

import argparse
import cv2
import logging
import sys
from pathlib import Path
from typing import Optional, Tuple

try:
    from pdf2image import convert_from_path
    import pytesseract
    from PIL import Image
    import numpy as np
except ImportError as e:
    print(f"Dependencia faltando: {e}")
    sys.exit(1)

# Importar modulos do projeto
sys.path.insert(0, str(Path(__file__).parent))
from main import (
    classify_document,
    extract_text_from_pdf_adaptive,
    extract_document_data,
    extract_fallback_data,
    find_tesseract_path,
    find_poppler_path,
    OCR_LANG,
    PROJECT_ROOT,
    TESSDATA_DIR,
)

# =============================================================================
# SETUP
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s"
)
logger = logging.getLogger(__name__)

# Tesseract paths
TESSERACT_PATH = find_tesseract_path()
POPPLER_PATH = find_poppler_path()
LOGS_DIR = PROJECT_ROOT / "logs"


# =============================================================================
# RECOMENDAÇÃO 2: OCR AGRESSIVO PARA EDGE CASES
# =============================================================================

class AggressiveOCRTester:
    """Testa OCR com múltiplos níveis de pré-processamento agressivo."""

    def __init__(self, tesseract_path: str, poppler_path: Optional[str]):
        self.tesseract_path = tesseract_path
        self.poppler_path = poppler_path

    def test_pdf(self, pdf_path: Path) -> dict:
        """Testa um PDF com múltiplas estratégias OCR."""
        logger.info(f"\n{'=' * 90}")
        logger.info(f"Testando: {pdf_path.name}")
        logger.info(f"{'=' * 90}")

        results = {
            "filename": pdf_path.name,
            "strategies": {},
            "classification_attempts": {},
        }

        # Estratégia 1: OCR Padrão (300 DPI)
        logger.info("\n[1] Estratégia Padrão (300 DPI):")
        text_standard, doc_type_standard = extract_text_from_pdf_adaptive(
            pdf_path, self.tesseract_path, self.poppler_path, logger
        )
        results["strategies"]["standard_300dpi"] = {
            "text_preview": text_standard[:300],
            "length": len(text_standard),
        }
        results["classification_attempts"]["standard_300dpi"] = {
            "doc_type": doc_type_standard,
            "method": "standard_ocr",
        }
        logger.info(f"  ✓ OCR OK - {len(text_standard)} chars")
        logger.info(f"  Classificação: {doc_type_standard or 'NENHUMA'}")

        # Estratégia 2: OCR Agressivo com Thresholding Adaptativo
        logger.info("\n[2] Estratégia Agressiva (Thresholding Adaptativo):")
        text_aggressive, doc_type_aggressive = self._ocr_with_aggressive_threshold(pdf_path)
        results["strategies"]["aggressive_threshold"] = {
            "text_preview": text_aggressive[:300],
            "length": len(text_aggressive),
        }
        results["classification_attempts"]["aggressive_threshold"] = {
            "doc_type": doc_type_aggressive,
            "method": "aggressive_threshold",
        }
        logger.info(f"  ✓ OCR OK - {len(text_aggressive)} chars")
        logger.info(f"  Classificação: {doc_type_aggressive or 'NENHUMA'}")

        # Estratégia 3: OCR com Erosão/Dilatação (morfologia)
        logger.info("\n[3] Estratégia Morfológica (Erosão/Dilatação):")
        text_morpho, doc_type_morpho = self._ocr_with_morphological_ops(pdf_path)
        results["strategies"]["morphological"] = {
            "text_preview": text_morpho[:300],
            "length": len(text_morpho),
        }
        results["classification_attempts"]["morphological"] = {
            "doc_type": doc_type_morpho,
            "method": "morphological",
        }
        logger.info(f"  ✓ OCR OK - {len(text_morpho)} chars")
        logger.info(f"  Classificação: {doc_type_morpho or 'NENHUMA'}")

        # Estratégia 4: OCR com Contraste Alto
        logger.info("\n[4] Estratégia Contraste Alto (CLAHE):")
        text_clahe, doc_type_clahe = self._ocr_with_clahe(pdf_path)
        results["strategies"]["clahe"] = {
            "text_preview": text_clahe[:300],
            "length": len(text_clahe),
        }
        results["classification_attempts"]["clahe"] = {
            "doc_type": doc_type_clahe,
            "method": "clahe",
        }
        logger.info(f"  ✓ OCR OK - {len(text_clahe)} chars")
        logger.info(f"  Classificação: {doc_type_clahe or 'NENHUMA'}")

        # Resumo
        classifications = [
            ("Padrão", doc_type_standard),
            ("Agressivo", doc_type_aggressive),
            ("Morfológico", doc_type_morpho),
            ("CLAHE", doc_type_clahe),
        ]

        best_match = None
        match_count = {}
        for method, doc_type in classifications:
            if doc_type:
                match_count[doc_type] = match_count.get(doc_type, 0) + 1
                if match_count[doc_type] > 1:
                    best_match = doc_type

        logger.info(f"\n{'─' * 90}")
        logger.info("RESUMO DE CLASSIFICAÇÕES:")
        for method, doc_type in classifications:
            status = "✓" if doc_type else "✗"
            logger.info(f"  {status} {method:20s} → {doc_type or 'NENHUMA'}")

        if best_match:
            logger.info(f"\n✅ CONSENSO: {best_match} (concordância em {match_count[best_match]}/4 estratégias)")
            results["consensus"] = best_match
        else:
            logger.warning(f"\n⚠️  SEM CONSENSO - Todas as estratégias retornaram tipos diferentes ou nada")
            results["consensus"] = None

        return results

    def _ocr_with_aggressive_threshold(self, pdf_path: Path) -> Tuple[str, Optional[str]]:
        """OCR com thresholding adaptativo."""
        try:
            images = convert_from_path(pdf_path, dpi=300, poppler_path=self.poppler_path)
            if not images:
                return "", None

            image = images[0]
            img_cv = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)
            gray = cv2.cvtColor(img_cv, cv2.COLOR_BGR2GRAY)

            # Thresholding adaptativo
            thresh = cv2.adaptiveThreshold(
                gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2
            )

            # Inverter se necessário
            if np.mean(thresh) < 128:
                thresh = cv2.bitwise_not(thresh)

            # OCR
            text = pytesseract.image_to_string(thresh, lang=OCR_LANG)
            text = text.strip()

            if text:
                doc_type = classify_document(text)
                return text, doc_type
            return "", None

        except Exception as e:
            logger.error(f"  ✗ Erro: {e}")
            return "", None

    def _ocr_with_morphological_ops(self, pdf_path: Path) -> Tuple[str, Optional[str]]:
        """OCR com operações morfológicas (erosão/dilatação)."""
        try:
            images = convert_from_path(pdf_path, dpi=300, poppler_path=self.poppler_path)
            if not images:
                return "", None

            image = images[0]
            img_cv = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)
            gray = cv2.cvtColor(img_cv, cv2.COLOR_BGR2GRAY)

            # Thresholding binário
            _, thresh = cv2.threshold(gray, 127, 255, cv2.THRESH_BINARY)

            # Operações morfológicas
            kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
            morph = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel, iterations=2)
            morph = cv2.morphologyEx(morph, cv2.MORPH_OPEN, kernel, iterations=1)

            # OCR
            text = pytesseract.image_to_string(morph, lang=OCR_LANG)
            text = text.strip()

            if text:
                doc_type = classify_document(text)
                return text, doc_type
            return "", None

        except Exception as e:
            logger.error(f"  ✗ Erro: {e}")
            return "", None

    def _ocr_with_clahe(self, pdf_path: Path) -> Tuple[str, Optional[str]]:
        """OCR com CLAHE (Contrast Limited Adaptive Histogram Equalization)."""
        try:
            images = convert_from_path(pdf_path, dpi=300, poppler_path=self.poppler_path)
            if not images:
                return "", None

            image = images[0]
            img_cv = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)
            gray = cv2.cvtColor(img_cv, cv2.COLOR_BGR2GRAY)

            # CLAHE
            clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
            enhanced = clahe.apply(gray)

            # Thresholding
            _, thresh = cv2.threshold(enhanced, 127, 255, cv2.THRESH_BINARY)

            # OCR
            text = pytesseract.image_to_string(thresh, lang=OCR_LANG)
            text = text.strip()

            if text:
                doc_type = classify_document(text)
                return text, doc_type
            return "", None

        except Exception as e:
            logger.error(f"  ✗ Erro: {e}")
            return "", None


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Testa PDFs com múltiplas estratégias OCR agressivas"
    )
    parser.add_argument(
        "--file",
        type=Path,
        help="Teste um PDF específico",
    )
    parser.add_argument(
        "--dir",
        type=Path,
        help="Teste todos os PDFs em um diretório",
    )
    parser.add_argument(
        "--unclassified-only",
        action="store_true",
        help="Teste apenas PDFs não classificados (baseado em monitor_confidence.json)",
    )
    args = parser.parse_args()

    if not TESSERACT_PATH:
        logger.error("Tesseract não encontrado!")
        sys.exit(1)

    tester = AggressiveOCRTester(TESSERACT_PATH, POPPLER_PATH)

    # Selecionar PDFs para testar
    pdfs_to_test = []

    if args.file:
        pdfs_to_test = [args.file]
    elif args.dir:
        pdfs_to_test = list(args.dir.glob("**/*.pdf"))
    elif args.unclassified_only:
        # Carregar monitor de confiança
        try:
            from monitor_confidence import ConfidenceMonitor
            monitor = ConfidenceMonitor()
            monitor.print_report()
            
            # Encontrar primeiros 3 PDFs de UNIDENTIFIED/low-confidence
            scanner_dir = Path(r"G:\RH\EQUIPE RH\ARQUIVO\SCANNER")
            if scanner_dir.exists():
                all_pdfs = list(scanner_dir.glob("*.pdf"))
                logger.info(f"\nEncontrados {len(all_pdfs)} PDFs no scanner.")
                pdfs_to_test = all_pdfs[:3]  # Trestar primeiros 3
                logger.info(f"Testando {len(pdfs_to_test)} PDFs...")
            else:
                logger.error(f"Diretório não encontrado: {scanner_dir}")
                sys.exit(1)
        except Exception as e:
            logger.error(f"Erro ao carregar monitor: {e}")
            sys.exit(1)
    else:
        # Default: testa 3 PDFs de exemplo
        test_dir = PROJECT_ROOT / "TEST PDFs"
        if test_dir.exists():
            pdfs_to_test = list(test_dir.glob("*.pdf"))[:3]
        else:
            logger.error("Nenhum PDF especificado. Use --file, --dir ou --unclassified-only")
            sys.exit(1)

    if not pdfs_to_test:
        logger.error("Nenhum PDF encontrado para testar.")
        sys.exit(1)

    logger.info(f"\n{'=' * 90}")
    logger.info(f"TESTE DE OCR AGRESSIVO PARA {len(pdfs_to_test)} PDF(s)")
    logger.info(f"{'=' * 90}")

    results = []
    for pdf_path in pdfs_to_test:
        if not pdf_path.exists():
            logger.warning(f"Arquivo não encontrado: {pdf_path}")
            continue

        result = tester.test_pdf(pdf_path)
        results.append(result)

    # Resumo final
    logger.info(f"\n{'=' * 90}")
    logger.info("RESUMO FINAL")
    logger.info(f"{'=' * 90}\n")

    for i, result in enumerate(results, 1):
        consensus = result.get("consensus") or "NENHUM"
        logger.info(f"{i}. {result['filename']:50s} → {consensus}")

    logger.info(f"\n{'=' * 90}")


if __name__ == "__main__":
    main()
