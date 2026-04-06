# AGENTS

## Project overview
- Single-script OCR renamer in `main.py` that reads PDFs, classifies doc type, extracts fields, and renames files in place.
- Data flow: PDF -> images (pdf2image) -> preprocess -> OCR (tesseract) -> normalize -> classify -> extract -> rename.
- All processing happens in the configured scanner folder root; no recursive subfolder scan.

## Key files and directories
- `main.py`: all logic (config, OCR pipeline, doc signatures, extractors, rename rules).
- `config.ini`: user-configured `scanner_dir` path (auto-created on first run).
- `tessdata/`: required Tesseract language data (expects `por.traineddata`).
- `logs/`: run logs created by `setup_logging()`.
- `TEST PDFs/`: sample input PDFs used during development.

## Running and dependencies
- Entry point: `python main.py` (expects Windows Tesseract + Poppler installed and discoverable).
- Dependency probe is in `find_tesseract_path()` and `find_poppler_path()`; adjust candidate paths there if installs differ.
- Python deps: `pytesseract`, `pdf2image`, `Pillow`, `opencv-python`, `numpy`, `tenacity`.
- Install with: `pip install pytesseract pdf2image Pillow opencv-python numpy tenacity`

## Performance and Reliability Optimizations (v2.0)
- **Image caching:** PDF converted once, images cached and reused across OCR passes (40-60% speedup).
- **Compiled regex:** Signatures compiled at startup with `COMPILED_SIGNATURES` (10% faster).
- **Parallelization:** `ThreadPoolExecutor(max_workers=3)` processes up to 3 PDFs simultaneously and uses `as_completed()` for streaming results (~3x faster for batches).
- **Retry logic:** `@retry` decorator from `tenacity` (3 attempts, exponential backoff) on `pdf_to_images()` and `ocr_image()` for transient I/O failures.
- **Environment validation:** `validate_environment()` tests Tesseract, Poppler, and folder permissions before processing.
- **Checkpoint/recovery:** `.checkpoint` file in `logs/` tracks processed files; rerun resumes from last successful PDF (useful for large batches that fail mid-way).


## Project-specific conventions
- Document types are identified by regex signatures in `DOC_TYPE_SIGNATURES` (e.g., `FMM`, `CP`, `FN`, `MBV`, `AP`, `NF`).
- OCR cleanup uses `OCR_CORRECTIONS` and `normalize_ocr_text()` before classification and extraction.
- Extractors are mapped in `EXTRACTORS` (e.g., `extract_fmm_data`, `extract_cp_data`) and return `name` + `period`.
- Output filename format is `TIPO - NOME - PERIODO.pdf` via `build_new_filename()`.
- Name/period normalization happens in `clean_name()` and `correct_year()`; keep changes consistent there.
- Conflicts are resolved by `resolve_filename_conflict()` adding `(1)`, `(2)`, etc.

## Integration points and edge behavior
- `pdf2image` uses Poppler; missing Poppler raises a hard error before processing starts.
- Unicode path issues on Windows are handled by a retry in `ocr_image()` without `--tessdata-dir`.
- Multi-pass OCR: default PSM 6, then PSM 4/3, then table/hi-res paths depending on quality and doc type.
- **Image caching** in `extract_text_from_pdf()`: images converted once at start, reused in all OCR passes via `preprocessed_default` and lazy-loaded `preprocessed_tables`.
- **Parallelized processing** in `main()` with `ThreadPoolExecutor`: futures submitted for all PDFs, results collected with `as_completed()` for streaming output.
- **Checkpoint system**: processed PDFs tracked in `logs/.checkpoint` (JSON set); rerun skips completed PDFs, clearing checkpoint only when all succeed with zero errors.
- **Tenacity retry**: wraps `pdf_to_images()` and `ocr_image()` to auto-retry 3x on any exception (handles transient Poppler/Tesseract hangs, file lock issues).
- **Environment validation** (`validate_environment()`) runs BEFORE any PDF processing; tests Tesseract version, language support (por), Poppler availability, and folder R/W perms.


