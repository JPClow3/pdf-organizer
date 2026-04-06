import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import main as scanner


class MonitorEdgeCasesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.logger = logging.getLogger("test-monitor")
        self.logger.handlers = []
        self.logger.addHandler(logging.NullHandler())

    def test_pdf_bloqueado_fica_adiado(self):
        with tempfile.TemporaryDirectory() as tmp:
            scanner_dir = Path(tmp)
            pdf_path = scanner_dir / "bloqueado.pdf"
            pdf_path.write_bytes(b"%PDF-1.4")

            with mock.patch.object(scanner, "validate_pdf_integrity", return_value=(False, True, "locked")):
                result = scanner.process_single_pdf(
                    pdf_path,
                    "tesseract",
                    None,
                    scanner_dir,
                    self.logger,
                    defer_on_transient=True,
                )

            self.assertEqual(result.status, scanner.ProcessStatus.DEFERRED)
            self.assertTrue(result.retryable_error)

    def test_pdf_corrompido_vira_erro(self):
        with tempfile.TemporaryDirectory() as tmp:
            scanner_dir = Path(tmp)
            pdf_path = scanner_dir / "corrompido.pdf"
            pdf_path.write_bytes(b"not-a-valid-pdf")

            with mock.patch.object(scanner, "validate_pdf_integrity", return_value=(False, False, "corrompido")):
                result = scanner.process_single_pdf(
                    pdf_path,
                    "tesseract",
                    None,
                    scanner_dir,
                    self.logger,
                    defer_on_transient=True,
                )

            self.assertEqual(result.status, scanner.ProcessStatus.ERROR)
            self.assertFalse(result.retryable_error)

    def test_duplicado_mesmo_nome_recebe_sufixo(self):
        with tempfile.TemporaryDirectory() as tmp:
            scanner_dir = Path(tmp)
            base = scanner_dir / "FMM - FULANO - 01-2026.pdf"
            base.write_bytes(b"a")

            resolved = scanner.resolve_filename_conflict(base)
            self.assertEqual(resolved.name, "FMM - FULANO - 01-2026 (1).pdf")

            resolved.write_bytes(b"b")
            resolved2 = scanner.resolve_filename_conflict(base)
            self.assertEqual(resolved2.name, "FMM - FULANO - 01-2026 (2).pdf")

    def test_restart_com_checkpoint_ignora_ja_processados(self):
        with tempfile.TemporaryDirectory() as tmp:
            scanner_dir = Path(tmp)
            logs_dir = scanner_dir / "logs"
            logs_dir.mkdir()
            checkpoint_file = logs_dir / ".checkpoint"

            ja_processado = scanner_dir / "ok.pdf"
            novo = scanner_dir / "novo.pdf"
            ja_processado.write_bytes(b"pdf-1")
            novo.write_bytes(b"pdf-2")

            key = scanner.build_checkpoint_key(ja_processado)
            scanner.save_checkpoint({key}, checkpoint_file)
            processed = scanner.load_checkpoint(checkpoint_file)

            pendentes = scanner.collect_pending_pdf_files(scanner_dir, processed)
            self.assertEqual([p.name for p in pendentes], ["novo.pdf"])

    def test_baixa_confianca_envia_para_revisao(self):
        with tempfile.TemporaryDirectory() as tmp:
            scanner_dir = Path(tmp)
            pdf_path = scanner_dir / "baixa_conf.pdf"
            pdf_path.write_bytes(b"%PDF-1.4")

            with mock.patch.object(scanner, "validate_pdf_integrity", return_value=(True, False, None)), \
                 mock.patch.object(scanner, "extract_text_from_pdf_adaptive", return_value=("texto", "CP")), \
                 mock.patch.object(scanner, "get_classification_confidence", return_value=35.0), \
                 mock.patch.object(scanner, "extract_document_data", return_value={"name": "JOAO SILVA", "period": "03-2026"}):
                result = scanner.process_single_pdf(
                    pdf_path,
                    "tesseract",
                    None,
                    scanner_dir,
                    self.logger,
                    defer_on_transient=False,
                    confidence_gate_enabled=True,
                    confidence_thresholds={"CP": 70.0},
                    confidence_baseline=70.0,
                )

            self.assertEqual(result.status, scanner.ProcessStatus.REVIEW)
            self.assertTrue((scanner_dir / scanner.REVIEW_DIR_NAME / "baixa_conf.pdf").exists())


if __name__ == "__main__":
    unittest.main()
