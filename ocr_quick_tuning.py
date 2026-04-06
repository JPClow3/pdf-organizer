#!/usr/bin/env python3
"""
Teste Rápido: Compara estratégias TOP para os PDFs problemáticos
Menor escopo: testa apenas as 12 combinações mais promissoras, não todas as 48
"""
import os
import time
import json
from pathlib import Path

os.environ["TESSDATA_PREFIX"] = str(Path(__file__).parent / "tessdata")

from main import (
    find_tesseract_path,
    find_poppler_path,
    pdf_to_images,
    preprocess_image,
    preprocess_image_for_tables,
    preprocess_image_light,
    ocr_image,
    build_ocr_config,
    normalize_ocr_text,
    classify_document,
    _text_quality_ratio,
)

PROJECT_ROOT = Path(__file__).resolve().parent
TEST_DIR = PROJECT_ROOT / "TEST PDFs"
OUT_FILE = PROJECT_ROOT / "ocr_quick_tuning.json"

# Estratégias TOP: baseadas em empirismo OCR comum
TOP_COMBOS = [
    (300, 6, "default"),  # PSM 6 + default preprocessing é o baseline
    (300, 4, "tables"),   # PSM 4 + table preprocessing para tabelas
    (300, 3, "default"),  # PSM 3 full auto
    (450, 6, "light"),    # Higher DPI + light preproc para manuscritos
    (450, 4, "default"),  # Higher DPI + default para tudo
    (600, 6, "default"),  # Muito alto DPI
    (300, 6, "tables"),   # Default DPI mas com table preproc
    (300, 6, "light"),    # Raw+light preprocessing
]

def apply_preproc(img, mode: str):
    if mode == "default":
        return preprocess_image(img)
    if mode == "tables":
        return preprocess_image_for_tables(img)
    if mode == "light":
        return preprocess_image_light(img)
    raise ValueError(f"Modo inválido: {mode}")


def test_combo(pdf_path: Path, tesseract_path: str, poppler_path: str | None, dpi: int, psm: int, preproc: str):
    """Testa uma combinação e retorna dict com métricas."""
    try:
        images = pdf_to_images(pdf_path, poppler_path, dpi=dpi)[:2]
        config = build_ocr_config(psm=psm)

        full_text = ""
        for img in images:
            processed = apply_preproc(img, preproc)
            page_text = ocr_image(processed, tesseract_path, config=config)
            full_text += "\n" + page_text

        normalized = normalize_ocr_text(full_text)
        doc_type = classify_document(normalized)
        quality = _text_quality_ratio(normalized)

        return {
            "success": True,
            "doc_type": doc_type,
            "classified": doc_type is not None,
            "quality": quality,
            "text_len": len(normalized),
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "doc_type": None,
            "classified": False,
            "quality": 0.0,
            "text_len": 0,
        }


def main():
    tesseract_path = find_tesseract_path()
    poppler_path = find_poppler_path()

    # PDFs problemáticos (aqueles que NÃO foram identificados no teste anterior)
    all_files = sorted(TEST_DIR.glob("*.pdf"))
    problematic = [p for p in all_files if not p.name.startswith(("FMM -", "CP -", "FN -", "AP -"))]
    
    if not problematic:
        print("Nenhum PDF problemático encontrado.")
        return

    print(f"PDFs problemáticos: {len(problematic)}")
    for p in problematic[:8]:
        print(f"  {p.name}")

    results = []
    combo_stats = {}

    print(f"\nTestando {len(TOP_COMBOS)} estratégias em {len(problematic[:6])} PDFs...")

    for dpi, psm, preproc in TOP_COMBOS:
        combo = f"dpi{dpi}_psm{psm}_{preproc}"
        combo_stats[combo] = {"classified": 0, "quality_sum": 0.0, "runs": 0}
        print(f"\n  {combo}:")

        for pdf_path in problematic[:6]:  # Limitar a 6 PDFs
            start = time.perf_counter()
            res = test_combo(pdf_path, tesseract_path, poppler_path, dpi, psm, preproc)
            elapsed = time.perf_counter() - start

            row = {
                "file": pdf_path.name,
                "combo": combo,
                "dpi": dpi,
                "psm": psm,
                "preproc": preproc,
                **res,
                "elapsed_s": elapsed,
            }
            results.append(row)

            combo_stats[combo]["runs"] += 1
            combo_stats[combo]["quality_sum"] += res["quality"]
            if res["classified"]:
                combo_stats[combo]["classified"] += 1

            status = "✓" if res["classified"] else "✗"
            print(f"    {status} {pdf_path.name[:40]:40} | tipo={str(res['doc_type']):7} | quality={res['quality']:.1%}")

    # Ranking
    ranking = []
    for combo, data in combo_stats.items():
        runs = data["runs"] or 1
        ranking.append({
            "combo": combo,
            "classified": data["classified"],
            "classification_rate": data["classified"] / runs,
            "avg_quality": data["quality_sum"] / runs,
            "runs": runs,
        })

    ranking.sort(key=lambda x: (x["classified"], x["avg_quality"]), reverse=True)

    print("\n=== TOP 5 ESTRATÉGIAS ===")
    for r in ranking[:5]:
        print(f"  {r['combo'].ljust(25)}: {r['classified']}/{r['runs']} classificados, "
              f"qualidade média={r['avg_quality']:.1%}")

    OUT_FILE.write_text(json.dumps({"ranking": ranking, "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nDetalhes salvos em: {OUT_FILE}")


if __name__ == "__main__":
    main()
