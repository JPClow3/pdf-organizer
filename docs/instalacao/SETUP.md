<!-- markdownlint-disable MD012 -->

# Instruções de Atualização - v2.1

## Instalação das Novas Dependências

O script depende de OCR + processamento de imagem + retry. Instale o pacote completo:

### Windows PowerShell

```powershell
# Opcao recomendada (idempotente)
pip install -r requirements.txt

# Opcao direta
pip install pytesseract pdf2image Pillow opencv-python numpy tenacity
```

## Monitoramento 24/7

```powershell
./install_monitor.ps1
```

Esse instalador registra duas tarefas no Agendador do Windows:

- `PDFScannerMonitor`: executa `main.py --watch` em loop continuo.
- `PDFScannerMonitor-Watchdog`: verifica heartbeat em `logs/.monitor_heartbeat.json` e reinicia o monitor quando detectar travamento silencioso ou queda.

O monitor ignora PDFs ainda em cópia ou bloqueados, envia falhas permanentes para `_QUARENTENA` e tenta novamente no próximo ciclo quando for um caso transitório.

Comandos recomendados:

```powershell
# Instalar com auto-deteccao de Python (.venv, VIRTUAL_ENV, py, python)
./install_monitor.ps1 -Action Install

# Instalar fixando interpretador no servidor
./install_monitor.ps1 -Action Install -PythonPath "C:\Python312\python.exe"

# Ver status de monitor + watchdog + idade do heartbeat
./install_monitor.ps1 -Action Status
```

## Treino OCR em Subpastas

O processamento operacional do `main.py` continua somente na raiz de `scanner_dir`, mas o treino pode varrer recursivamente todas as subpastas:

```powershell
# Treino com gravação de modelos aprendidos
python ocr_train_recursive.py

# Simulação sem gravar (somente relatório)
python ocr_train_recursive.py --dry-run
```

Saídas do treino:

- `ocr_training_recursive_report.json` com estatísticas e novos tipos detectados.
- `models/custom_models.json` com assinaturas de modelos novos (carregado automaticamente pelo `main.py`).

### Ajustes finos do monitor (config.ini)

```ini
[monitor]
watch_interval_seconds = 15
file_stability_seconds = 5
file_stability_checks = 3
deferred_max_attempts = 3
deferred_retry_cooldown_seconds = 30
watch_max_workers = 3
metrics_log_every_cycles = 4
quarantine_permanent_errors = true
confidence_gate_enabled = true

[confidence]
baseline = 70
fmm = 75
cp = 70
fn = 70
mbv = 80
ap = 70
nf = 70
recibo = 70
declaracao = 70
contrato = 75
comprovante = 70
gen = 70
```

Quando `confidence_gate_enabled = true`, arquivos com confiança abaixo do mínimo por tipo são movidos para `_REVISAO` em vez de serem renomeados automaticamente.

Quando `metrics_log_every_cycles = 4`, o monitor emite métricas de ciclo a cada 4 ciclos ociosos (e sempre que houver processamento): tempo médio por arquivo, taxa de adiados e taxa de revisão.

- **Recovery:** Salva checkpoint em `logs/.checkpoint` de forma atômica para retomar onde parou

```powershell
python main.py --watch
```

### Verificar Instalação

```powershell
python -c "import tenacity; print(f'Tenacity {tenacity.__version__} instalado com sucesso')"
```

## Atualizações Principais (v1.0 → v2.0)

### 🚀 Performance

1. **Cache de Imagens:** PDF convertido 1x ao invés de 4x (~40% mais rápido)
2. **Regex Compilado:** Padrões compilam no startup (~10% mais rápido)
3. **Paralelização:** Processa até 3 PDFs simultaneamente (~3x mais rápido para lotes grandes)

### 🛡️ Reliability

1. **Retry Automático:** Tenta até 3x em caso de falha I/O com backoff exponencial
2. **Validação Antecipada:** Testa dependências antes de gastar tempo com OCR
3. **Recovery:** Salva checkpoint em `logs/.checkpoint` de forma atômica para retomar onde parou

## Compatibilidade

- ✅ Retrocompatível com `config.ini` existente
- ✅ Não quebra scripts/chamadas existentes
- ✅ Apenas melhora performance e reliability
- ✅ Logs detalhados continuam em `logs/scanner_log_*.txt`

## Testar Antes de usar em Produção

```powershell
# Copie um PDF teste para TEST PDFs/
Copy-Item "path\to\test.pdf" ".\TEST PDFs\"

# Execute uma vez para validar tudo
python main.py

# Verifique logs
Get-Content "logs\scanner_log_*.txt" -Tail 20
```

## Problemas Comuns

### `ModuleNotFoundError: No module named 'tenacity'`

```powershell
pip install -r requirements.txt --upgrade
```

### ThreadPoolExecutor muito agressivo (CPU 100%)

Em `main()`, reduza `max_workers`:

```python
num_workers = min(2, len(pdf_files))  # Alterar de 3 para 2
```

### Checkpoint criado mas não removido

Limpe manualmente:

```powershell
Remove-Item "logs\.checkpoint" -Force
```

### Tarefa do monitor não sobe

Rode novamente fixando o Python explicitamente:

```powershell
./install_monitor.ps1 -Action Install -PythonPath "C:\Python312\python.exe"
```

### Monitor parou de processar sem cair

Verifique a idade do heartbeat no status:

```powershell
./install_monitor.ps1 -Action Status
```

Se estiver acima do timeout configurado, o watchdog reinicia automaticamente.

---

**Pronto!** Execute `python main.py` para começar a desfrutar dos ganhos de performance e reliability.

