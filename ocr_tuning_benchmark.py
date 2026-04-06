import os
import time
import json
from pathlib import Path
from collections import defaultdict

from main import (
    TESSDATA_DIR,
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

os.environ["TESSDATA_PREFIX"] = str(TESSDATA_DIR)

PROJECT_ROOT = Path(__file__).resolve().parent
TEST_DIR = PROJECT_ROOT / "TEST PDFs"
OUT_FILE = PROJECT_ROOT / "ocr_tuning_results.json"

DOC_PREFIXES = ("FMM -", "CP -", "FN -", "MBV -", "AP -", "NF -")

DPI_OPTIONS = [300, 450, 600]
PSM_OPTIONS = [6, 4, 3]
PREPROC_OPTIONS = ["raw", "default", "tables", "light"]
MAX_FILES = 12
MAX_PAGES = 2


def get_problematic_files() -> list[Path]:
    files = sorted(TEST_DIR.glob("*.pdf"))
    problematic = [p for p in files if not p.name.startswith(DOC_PREFIXES)]
    return problematic[:MAX_FILES]


def apply_preproc(img, mode: str):
    if mode == "raw":
        return img
    if mode == "default":
        return preprocess_image(img)
    if mode == "tables":
        return preprocess_image_for_tables(img)
    if mode == "light":
        return preprocess_image_light(img)
    raise ValueError(f"Modo invalido: {mode}")


def run_combo(pdf_path: Path, tesseract_path: str, poppler_path: str | None, dpi: int, psm: int, preproc: str):
    images = pdf_to_images(pdf_path, poppler_path, dpi=dpi)[:MAX_PAGES]
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
        "doc_type": doc_type,
        "quality": quality,
        "text_len": len(normalized),
    }


def main():
    tesseract_path = find_tesseract_path()
    poppler_path = find_poppler_path()

    files = get_problematic_files()
    if not files:
        print("Nenhum arquivo problematico encontrado.")
        return

    print(f"Arquivos problematicos analisados: {len(files)}")
    for f in files:
        print(f" - {f.name}")

    images_cache: dict[tuple[str, int], list] = {}
    file_errors = {}

    all_results = []
    aggregate = defaultdict(lambda: {"classified": 0, "quality_sum": 0.0, "runs": 0, "time_sum": 0.0})

    for dpi in DPI_OPTIONS:
        for psm in PSM_OPTIONS:
            for preproc in PREPROC_OPTIONS:
                combo = f"dpi{dpi}_psm{psm}_{preproc}"
                print(f"\nTestando combo: {combo}")
                for pdf_path in files:
                    key = (str(pdf_path), dpi)
                    start = time.perf_counter()
                    try:
                        # Cache de conversao PDF->imagem por arquivo+dpi
                        if key not in images_cache:
                            images_cache[key] = pdf_to_images(pdf_path, poppler_path, dpi=dpi)[:MAX_PAGES]

                        config = build_ocr_config(psm=psm)
                        full_text = ""
                        for img in images_cache[key]:
                            processed = apply_preproc(img, preproc)
                            page_text = ocr_image(processed, tesseract_path, config=config)
                            full_text += "\n" + page_text

                        normalized = normalize_ocr_text(full_text)
                        doc_type = classify_document(normalized)
                        quality = _text_quality_ratio(normalized)
                        elapsed = time.perf_counter() - start

                        row = {
                            "file": pdf_path.name,
                            "combo": combo,
                            "dpi": dpi,
                            "psm": psm,
                            "preproc": preproc,
                            "doc_type": doc_type,
                            "classified": doc_type is not None,
                            "quality": quality,
                            "text_len": len(normalized),
                            "elapsed_s": elapsed,
                        }
                        all_results.append(row)

                        aggregate[combo]["runs"] += 1
                        aggregate[combo]["quality_sum"] += quality
                        aggregate[combo]["time_sum"] += elapsed
                        if doc_type is not None:
                            aggregate[combo]["classified"] += 1

                    except Exception as e:
                        file_errors[pdf_path.name] = str(e)
                        elapsed = time.perf_counter() - start
                        row = {
                            "file": pdf_path.name,
                            "combo": combo,
                            "dpi": dpi,
                            "psm": psm,
                            "preproc": preproc,
                            "doc_type": None,
                            "classified": False,
                            "quality": 0.0,
                            "text_len": 0,
                            "elapsed_s": elapsed,
                            "error": str(e),
                        }
                        all_results.append(row)
                        aggregate[combo]["runs"] += 1
                        aggregate[combo]["time_sum"] += elapsed

    ranking = []
    for combo, data in aggregate.items():
        runs = data["runs"] or 1
        ranking.append({
            "combo": combo,
            "classified": data["classified"],
            "classification_rate": data["classified"] / runs,
            "avg_quality": data["quality_sum"] / runs,
            "avg_time_s": data["time_sum"] / runs,
            "runs": runs,
        })

    ranking.sort(key=lambda x: (x["classified"], x["avg_quality"], -x["avg_time_s"]), reverse=True)

    payload = {
        "tested_files": [f.name for f in files],
        "errors": file_errors,
        "ranking": ranking[:12],
        "results": all_results,
    }

    OUT_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n=== TOP ESTRATEGIAS ===")
    for r in ranking[:8]:
        print(
            f"{r['combo']}: classificados={r['classified']}/{r['runs']} "
            f"qualidade={r['avg_quality']:.3f} tempo={r['avg_time_s']:.2f}s"
        )

    print(f"\nResultado salvo em: {OUT_FILE}")


if __name__ == "__main__":
    main()
