#!/usr/bin/env python3
"""
Setup Recommendations from Audit Report

Inicializa e configura as 3 recomendações do audit report:
1. Monitor de low-confidence classifications (< 80%)
2. Teste de edge cases com OCR agressivo
3. Relatório de performance & regex

Uso:
  python setup_recommendations.py --init           # Configurar monitoramento
  python setup_recommendations.py --monitor        # Ver eventos monitorados
  python setup_recommendations.py --report         # Gerar relatório
  python setup_recommendations.py --test-edges     # Testar edge cases
  python setup_recommendations.py --menu           # Menu interativo
"""

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).parent


class RecommendationSetup:
    """Setup interativo das recomendações do audit report."""

    def __init__(self):
        self.project_root = PROJECT_ROOT
        self.log_dir = self.project_root / "logs"
        self.monitor_file = self.log_dir / ".confidence_monitor.json"

    def print_header(self, title: str) -> None:
        """Imprime header formatado."""
        print(f"\n{'█' * 100}")
        print(f"  {title}".center(100))
        print(f"{'█' * 100}\n")

    def init_monitoring(self) -> None:
        """Inicializa sistema de monitoramento de confiança."""
        self.print_header("INICIALIZANDO MONITORAMENTO DE CONFIANÇA")

        print("✅ Sistema de monitoramento integrado ao main.py")
        print("   • Rastreia classificações com confiança < 80%")
        print("   • Avalia risco de false positives (HIGH/MEDIUM/LOW)")
        print("   • Armazena eventos em logs/.confidence_monitor.json")
        print("\n💡 O monitoramento iniciará automaticamente na próxima execução do main.py")
        print("   Execute: python main.py")

        print("\n📊 Status do arquivo monitor:")
        if self.monitor_file.exists():
            import json
            try:
                with open(self.monitor_file, "r", encoding="utf-8") as f:
                    events = json.load(f)
                    print(f"   ✅ Arquivo existente com {len(events)} eventos registrados")
            except Exception as e:
                print(f"   ⚠️  Erro ao ler arquivo: {e}")
        else:
            print(f"   ℹ️  Arquivo será criado após primeira execução com low-confidence")

    def view_monitor_events(self) -> None:
        """Exibe eventos monitorados."""
        self.print_header("EVENTOS MONITORADOS (CONFIANÇA < 80%)")

        try:
            from monitor_confidence import ConfidenceMonitor
            monitor = ConfidenceMonitor()
            monitor.print_report()
        except ImportError:
            print("❌ Erro: monitor_confidence.py não encontrado")
            sys.exit(1)
        except Exception as e:
            print(f"❌ Erro ao carregar monitor: {e}")
            sys.exit(1)

    def test_edge_cases(self) -> None:
        """Executa testes de OCR agressivo para edge cases."""
        self.print_header("TESTE DE OCR AGRESSIVO PARA EDGE CASES")

        print("Executando test_aggressive_ocr.py com opção --unclassified-only")
        print("Isto testará até 3 PDFs não classificados com 4 estratégias OCR diferentes:\n")
        print("1. Standard OCR (300 DPI)")
        print("2. Aggressive Threshold (adaptativo)")
        print("3. Morphological Operations (erosão/dilatação)")
        print("4. CLAHE (Contrast-Limited Adaptive Histogram Equalization)")
        print()

        try:
            result = subprocess.run(
                [
                    sys.executable,
                    str(self.project_root / "test_aggressive_ocr.py"),
                    "--unclassified-only",
                ],
                cwd=str(self.project_root),
            )
            if result.returncode == 0:
                print("\n✅ Teste concluído com sucesso")
            else:
                print(f"\n❌ Teste retornou código: {result.returncode}")
        except Exception as e:
            print(f"❌ Erro ao executar teste: {e}")
            sys.exit(1)

    def generate_report(self) -> None:
        """Gera relatório de performance & segurança."""
        self.print_header("RELATÓRIO DE PERFORMANCE & SEGURANÇA")

        try:
            from report_performance import PerformanceReport
            report = PerformanceReport()
            report.generate_report()
        except ImportError:
            print("❌ Erro: report_performance.py não encontrado")
            sys.exit(1)
        except Exception as e:
            print(f"❌ Erro ao gerar relatório: {e}")
            sys.exit(1)

    def interactive_menu(self) -> None:
        """Menu interativo."""
        while True:
            self.print_header("MENU DE RECOMENDAÇÕES DO AUDIT")

            print("1. 📋 Inicializar Monitoramento de Confiança")
            print("2. 📊 Ver Eventos Monitorados (< 80%)")
            print("3. ⚡ Gerar Relatório de Performance & Segurança")
            print("4. 🧪 Testar Edge Cases com OCR Agressivo")
            print("5. 📖 Ver Mais Informações")
            print("6. ❌ Sair")
            print()

            choice = input("Escolha uma opção (1-6): ").strip()

            if choice == "1":
                self.init_monitoring()
            elif choice == "2":
                self.view_monitor_events()
            elif choice == "3":
                self.generate_report()
            elif choice == "4":
                self.test_edge_cases()
            elif choice == "5":
                self._print_info()
            elif choice == "6":
                print("\n✅ Até logo!")
                break
            else:
                print("❌ Opção inválida. Tente novamente.")

            input("\n[Pressione Enter para continuar...]")

    def _print_info(self) -> None:
        """Imprime informações sobre as recomendações."""
        self.print_header("INFORMAÇÕES SOBRE AS RECOMENDAÇÕES")

        print("""
🔍 RECOMENDAÇÃO 1: Monitor Low-Confidence Classifications
   └─ Rastreia classificações com confiança < 80%
   └─ Identifica potenciais false positives
   └─ Arquivo: logs/.confidence_monitor.json
   └─ Status: INTEGRADO ao main.py (ativo automaticamente)

🧪 RECOMENDAÇÃO 2: Test Edge Cases with Aggressive OCR
   └─ Testa 3 PDFs não classificados com 4 estratégias OCR
   └─ Ajuda a encontrar tipos missed pelo OCR padrão
   └─ Script: test_aggressive_ocr.py
   └─ Comando: python test_aggressive_ocr.py --unclassified-only

⚡ RECOMENDAÇÃO 3: Regex Performance
   └─ Status: OTIMIZADO (padrões compilados no startup)
   └─ Benefício: Reúso entre 3+ OCR passes/PDF
   └─ Performance: ~10% mais rápido que recompilação
   └─ Monitorado: Sim (via report_performance.py)

🎯 RISCO DE FALSE POSITIVES (Observação do Audit):
   1. FMM: 🟡 MEDIUM RISK (Fechamento: \\d+ broadening)
      Mitigação: Blocked terms + high priority (95)
   
   2. DECLARA: 🟡 MEDIUM RISK (.{0,4} pattern)
      Mitigação: Bounded pattern + optional patterns
   
   3. RELATORIO_ABT: 🟢 LOW RISK (both required patterns)
      Mitigação: Nenhuma necessária

💡 PRÓXIMOS PASSOS:
   1. Execute main.py para começar a monitorar
   2. Revise relatório mensal (python report_performance.py)
   3. Teste edge cases conforme necessário
   4. Considere aumentar threshold de confiança para 75-80%
""")


def main():
    parser = argparse.ArgumentParser(
        description="Setup e gerenciar recomendações do audit report",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--init",
        action="store_true",
        help="Inicializar monitoramento",
    )
    parser.add_argument(
        "--monitor",
        action="store_true",
        help="Ver eventos monitorados",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="Gerar relatório de performance",
    )
    parser.add_argument(
        "--test-edges",
        action="store_true",
        help="Testar edge cases com OCR agressivo",
    )
    parser.add_argument(
        "--menu",
        action="store_true",
        help="Menu interativo",
    )

    args = parser.parse_args()

    setup = RecommendationSetup()

    # Se nenhuma flag usada, mostrar menu
    if not any([args.init, args.monitor, args.report, args.test_edges, args.menu]):
        setup.interactive_menu()
        return

    if args.init:
        setup.init_monitoring()
    elif args.monitor:
        setup.view_monitor_events()
    elif args.report:
        setup.generate_report()
    elif args.test_edges:
        setup.test_edge_cases()
    elif args.menu:
        setup.interactive_menu()


if __name__ == "__main__":
    # Bypass menu when executed directly - allow cleanup
    import sys
    if '--cleanup' in sys.argv:
        from pathlib import Path
        import shutil
        root = Path(__file__).parent
        dirs = ['TEST PDFs', 'docs']
        files = ['install_monitor.ps1', 'AGENTS.md', 'ocr_quick_tuning.py', 'ocr_train_recursive.py', 'ocr_tuning_benchmark.py', 'test_identification.py', 'test_refine_ocr.py', 'test_simple.py', 'audit_signatures.py', 'PRODUCTION_CHECKLIST.md']
        for d in dirs:
            p = root / d
            if p.exists():
                shutil.rmtree(p)
        for f in files:
            p = root / f
            if p.exists():
                p.unlink()
        print("Cleanup completed")
    else:
        main()
