#!/usr/bin/env python3
"""
Final production validation suite.

This script is intended to be an honest release gate:
- any failed mandatory check exits non-zero
- the summary is derived from actual results
- optional runtime dependencies are reported as warnings, not hidden
"""

import sys
from pathlib import Path


workspace = Path(__file__).parent
sys.path.insert(0, str(workspace))

if sys.platform == "win32":
    import io

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")


def report(status: str, message: str) -> None:
    print(f"  [{status}] {message}")


def main() -> int:
    print("=" * 70)
    print("FINAL PRODUCTION VALIDATION TEST SUITE")
    print("=" * 70)
    print()

    failed_checks: list[str] = []
    warnings: list[str] = []

    print("[1/6] Testing imports...")
    try:
        from main import (  # pylint: disable=import-outside-toplevel
            classify_document,
            _extract_date_from_text,
            EXTRACTORS,
            _ocr_digits,
            CONFIDENCE_MONITOR_AVAILABLE,
            CONFIDENCE_MONITOR_IMPORT_ERROR,
            MBV_TEMPLATE_DIR,
            MBV_TEMPLATE_FILES,
        )
        _ = classify_document
        report("OK", "All core functions imported successfully")
    except ImportError as exc:
        report("ERROR", f"Import failed: {exc}")
        return 1

    print("\n[2/6] Testing date extraction function...")
    test_dates = [
        ("09/12/2023", True, "DD/MM/YYYY format"),
        ("09-12-2023", True, "DD-MM-YYYY format"),
        ("09 de dezembro de 2023", True, "Brazilian text format"),
        ("Setembro/2023", True, "Month/Year format"),
        ("Assinatura do empregado / setor", False, "False positive check"),
        ("12/2023", True, "MM/YYYY format"),
        ("DEZ/2023", True, "Month abbrev/YYYY"),
    ]

    date_passed = 0
    for test_str, should_match, desc in test_dates:
        result = _extract_date_from_text(test_str)
        matched = result is not None
        if matched == should_match:
            report("OK", f"{desc}: '{test_str}' -> {'matched' if matched else 'no match'}")
            date_passed += 1
        else:
            report("ERROR", f"{desc}: '{test_str}' -> expected {should_match}, got {matched}")
            failed_checks.append(f"date extraction: {desc}")

    print("\n[3/6] Testing OCR digit correction...")
    test_ocr_cases = [
        ("CPF 123.456", "123456", "Mixed punctuation should preserve only digits"),
        ("abc", "", "Strings without OCR digits should return empty"),
        ("O1S", "015", "Common OCR substitutions should map to digits"),
    ]

    ocr_passed = 0
    for test_input, expected, desc in test_ocr_cases:
        result = _ocr_digits(test_input)
        if result == expected:
            report("OK", f"{desc}: '{test_input}' -> '{result}'")
            ocr_passed += 1
        else:
            report("ERROR", f"{desc}: '{test_input}' -> '{result}' (expected '{expected}')")
            failed_checks.append(f"ocr digit correction: {desc}")

    print("\n[4/6] Validating document extractors and optional runtime assets...")
    doc_types_to_check = ["FMM", "CP", "FN", "MBV", "AP", "ASO_ADMISSIONAL", "ATESTADO_MEDICO"]
    extractor_count = len(EXTRACTORS)

    report("OK", f"Total extractors configured: {extractor_count}")
    for doc_type in doc_types_to_check:
        if doc_type in EXTRACTORS:
            report("OK", f"{doc_type}: extractor configured")
        else:
            report("ERROR", f"{doc_type}: extractor missing")
            failed_checks.append(f"missing extractor: {doc_type}")

    if extractor_count < 10:
        report("ERROR", f"Only {extractor_count} extractors configured (expected at least 10)")
        failed_checks.append("extractor count below minimum")

    if CONFIDENCE_MONITOR_AVAILABLE:
        report("OK", "Optional confidence monitor module available")
    else:
        warning = CONFIDENCE_MONITOR_IMPORT_ERROR or "Confidence monitor unavailable"
        report("WARN", warning)
        warnings.append(warning)

    missing_templates = [
        filename
        for filename in MBV_TEMPLATE_FILES.values()
        if not (MBV_TEMPLATE_DIR / filename).is_file()
    ]
    if missing_templates:
        warning = (
            f"MBV templates missing in {MBV_TEMPLATE_DIR}: "
            + ", ".join(missing_templates)
            + ". MBV extraction will run in fallback ROI-only mode."
        )
        report("WARN", warning)
        warnings.append(warning)
    else:
        report("OK", "All MBV templates found")

    print("\n[5/6] Validating configuration and repository layout...")
    config_path = workspace / "config.ini"
    if not config_path.exists():
        report("ERROR", "config.ini NOT found")
        failed_checks.append("config.ini missing")
    else:
        report("OK", "config.ini found")
        content = config_path.read_text(encoding="utf-8")
        if "[paths]" in content and "[monitor]" in content and "[confidence]" in content:
            report("OK", "config.ini structure valid")
        else:
            report("ERROR", "config.ini missing one or more required sections")
            failed_checks.append("config.ini incomplete")

    required_entries = {
        "main.py": "Core application",
        "config.ini": "Configuration",
        "requirements.txt": "Dependencies",
        "README.md": "Documentation",
        "models": "Trained models directory",
        "tessdata": "OCR data directory",
    }
    for name, description in required_entries.items():
        path = workspace / name
        if path.exists():
            report("OK", f"{name}: present ({description})")
        else:
            report("ERROR", f"{name}: missing ({description})")
            failed_checks.append(f"missing required path: {name}")

    sample_dir = workspace / "sample"
    if sample_dir.exists():
        sample_count = len(list(sample_dir.glob("*.pdf")))
        report("OK", f"sample/: present with {sample_count} PDF(s)")
    else:
        report("WARN", "sample/ folder not found; no smoke corpus available")
        warnings.append("sample/ folder not found")

    print("\n[6/6] Summary...")
    print("\n" + "=" * 70)
    print("TEST RESULTS SUMMARY")
    print("=" * 70)
    print()
    print(f"[OK] Import validation: PASSED")
    print(f"[{'OK' if date_passed == len(test_dates) else 'ERROR'}] Date extraction: {date_passed}/{len(test_dates)} passed")
    print(f"[{'OK' if ocr_passed == len(test_ocr_cases) else 'ERROR'}] OCR digit correction: {ocr_passed}/{len(test_ocr_cases)} passed")
    print(f"[OK] Document extractors configured: {extractor_count}")
    print(f"[{'WARN' if warnings else 'OK'}] Warnings: {len(warnings)}")
    print(f"[{'ERROR' if failed_checks else 'OK'}] Failures: {len(failed_checks)}")

    if warnings:
        print("\nWarnings:")
        for warning in warnings:
            print(f"  - {warning}")

    if failed_checks:
        print("\nFailed checks:")
        for failure in failed_checks:
            print(f"  - {failure}")
        print("\n" + "=" * 70)
        print("[FAILED] VALIDATION FAILED - NOT READY FOR RELEASE")
        print("=" * 70)
        return 1

    print("\n" + "=" * 70)
    print("[COMPLETE] VALIDATION PASSED")
    print("=" * 70)
    if warnings:
        print("Release note: mandatory checks passed, but optional warnings remain.")
    else:
        print("Release note: all mandatory checks passed with no warnings.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
