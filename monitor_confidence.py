#!/usr/bin/env python3
"""
Monitor Low-Confidence Classifications
Rastreia classificações com confiança < 80% para detecção de false positives.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, asdict

# =============================================================================
# RECOMENDAÇÃO 1: MONITOR LOW-CONFIDENCE CLASSIFICATIONS
# =============================================================================

LOW_CONFIDENCE_THRESHOLD = 80.0  # Limiar de monitoramento (em %)
MONITOR_LOG_FILE = Path(__file__).parent / "logs" / ".confidence_monitor.json"


@dataclass
class LowConfidenceEvent:
    """Evento de classificação com confiança baixa."""
    timestamp: str
    filename: str
    doc_type: Optional[str]
    confidence_score: float
    extracted_name: Optional[str]
    extracted_period: Optional[str]
    ocr_preview: str
    false_positive_risk: str  # HIGH, MEDIUM, LOW


class ConfidenceMonitor:
    """Monitora classificações com confiança baixa para detecção de false positives."""

    def __init__(self, log_file: Optional[Path] = None):
        self.log_file = log_file or MONITOR_LOG_FILE
        self.events: list[LowConfidenceEvent] = []
        self._load_existing_events()

    def _load_existing_events(self):
        """Carrega eventos anteriores do arquivo JSON."""
        if self.log_file.exists():
            try:
                with open(self.log_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        self.events = [LowConfidenceEvent(**e) for e in data]
            except Exception as e:
                print(f"Aviso: Não foi possível carregar monitor existente: {e}")

    def log_low_confidence(
        self,
        filename: str,
        doc_type: Optional[str],
        confidence_score: float,
        extracted_name: Optional[str],
        extracted_period: Optional[str],
        ocr_preview: str,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        """Registra uma classificação com confiança baixa."""
        
        # Avaliar risco de false positive baseado no tipo de documento
        risk = self._assess_false_positive_risk(doc_type, confidence_score)
        
        event = LowConfidenceEvent(
            timestamp=datetime.now().isoformat(),
            filename=filename,
            doc_type=doc_type or "UNIDENTIFIED",
            confidence_score=confidence_score,
            extracted_name=extracted_name,
            extracted_period=extracted_period,
            ocr_preview=ocr_preview[:200],  # Primeiros 200 chars
            false_positive_risk=risk,
        )
        
        self.events.append(event)
        
        # Log to file
        self._save_events()
        
        # Log a console se logger disponível
        if logger:
            msg = (
                f"⚠️  LOW-CONFIDENCE ALERT: {filename} | "
                f"Type={doc_type} | Score={confidence_score:.1f}% | "
                f"Risk={risk}"
            )
            logger.warning(msg)

    def _assess_false_positive_risk(self, doc_type: Optional[str], score: float) -> str:
        """
        Avalia risco de false positive baseado em:
        - Tipo de documento (FMM é MEDIUM risk por "Fechamento: \\d+" broadening)
        - DECLARA é MEDIUM risk por .{0,4} pattern
        - Score baixo (50-80)
        """
        if score >= 80:
            return "LOW"
        
        if score >= 70:
            if doc_type in {"FMM", "DECLARA"}:
                return "MEDIUM"
            return "LOW"
        
        # < 70%
        if doc_type in {"FMM", "DECLARA"}:
            return "HIGH"
        if doc_type == "UNIDENTIFIED":
            return "MEDIUM"
        
        return "MEDIUM"

    def _save_events(self) -> None:
        """Salva eventos em arquivo JSON."""
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.log_file, "w", encoding="utf-8") as f:
            json.dump([asdict(e) for e in self.events], f, indent=2, ensure_ascii=False)

    def get_summary(self) -> dict:
        """Retorna resumo de eventos monitorados."""
        if not self.events:
            return {"total": 0, "by_type": {}, "by_risk": {}}
        
        by_type = {}
        by_risk = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
        
        for event in self.events:
            by_type[event.doc_type] = by_type.get(event.doc_type, 0) + 1
            by_risk[event.false_positive_risk] = by_risk.get(event.false_positive_risk, 0) + 1
        
        return {
            "total": len(self.events),
            "by_type": by_type,
            "by_risk": by_risk,
            "avg_score": sum(e.confidence_score for e in self.events) / len(self.events),
        }

    def print_report(self) -> None:
        """Imprime relatório de eventos monitorados."""
        summary = self.get_summary()
        
        print("\n" + "=" * 100)
        print("LOW-CONFIDENCE CLASSIFICATION MONITOR")
        print("=" * 100)
        print(f"\nTotal de eventos: {summary['total']}")
        print(f"Score médio: {summary.get('avg_score', 0):.1f}%")
        
        print("\nPor tipo de documento:")
        for doc_type, count in sorted(summary.get("by_type", {}).items()):
            print(f"  {doc_type}: {count}")
        
        print("\nPor risco de false positive:")
        for risk_level, count in sorted(summary.get("by_risk", {}).items()):
            print(f"  {risk_level}: {count}")
        
        if self.events:
            print("\n" + "-" * 100)
            print("ÚLTIMOS 5 EVENTOS:")
            for event in self.events[-5:]:
                print(
                    f"\n  {event.timestamp} | {event.filename}\n"
                    f"    Type: {event.doc_type} | Score: {event.confidence_score:.1f}% | Risk: {event.false_positive_risk}\n"
                    f"    Name: {event.extracted_name} | Period: {event.extracted_period}\n"
                    f"    Preview: {event.ocr_preview[:100]}..."
                )
        
        print("\n" + "=" * 100)


def create_monitor() -> ConfidenceMonitor:
    """Factory para criar monitor."""
    return ConfidenceMonitor()


if __name__ == "__main__":
    # Teste da funcionalidade
    monitor = create_monitor()
    monitor.print_report()
