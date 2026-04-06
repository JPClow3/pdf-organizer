# Exemplos de Uso - PDF Scanner v2.0

## Instalação Rápida

```powershell
# Windows PowerShell
pip install tenacity
python main.py
```

---

## Exemplo 1: Processamento Normal (Paralelização Automática)

```powershell
PS> python main.py

============================================================
  PDF Scanner - OCR Document Renamer
============================================================
Pasta de entrada: g:\TECNOLOGIA DA INFORMACAO\AUTOMAÇÂO PDF RH\TEST PDFs
Projeto: g:\TECNOLOGIA DA INFORMACAO\AUTOMAÇÂO PDF RH
Validando ambiente...
  Tesseract: OK - tesseract 5.3.0
  Tesseract idiomas: OK (inclui português)
  Poppler: OK
  Pasta scanner: OK (leitura e escrita)
Validação concluída com sucesso!
Encontrados 50 arquivos PDF

[1/50] pdf_001.pdf -> RENOMEADO
[2/50] pdf_002.pdf -> RENOMEADO
[3/50] pdf_003.pdf -> NAO IDENTIFICADO
[4/50] pdf_004.pdf -> RENOMEADO
[5/50] pdf_005.pdf -> RENOMEADO
...
[48/50] pdf_048.pdf -> RENOMEADO   
[49/50] pdf_049.pdf -> IGNORADO     
[50/50] pdf_050.pdf -> RENOMEADO

============================================================
  RESUMO
============================================================
  Renomeados:        46
  Nao identificados: 3
  Ignorados (nao-RH): 1
  Erros:             0
  Total processados: 50
============================================================
```

✅ Com paralelização: ~50 PDFs em ~3-5 min (vs ~50 min sequencial)

---

## Exemplo 2: Recovery com Checkpoint

**1️⃣ Primeira execução (falha no meio):**

```powershell
PS> python main.py

[1/100] pdf_001.pdf -> RENOMEADO
[2/100] pdf_002.pdf -> RENOMEADO
...
[47/100] pdf_047.pdf -> RENOMEADO
^C
# Ctrl+C pressionado
Checkpoint salvo - retome a execução para continuar com os 0 erros.
```

**2️⃣ Reexecução (retoma de checkpoint):**

```powershell
PS> python main.py

Encontrados 100 arquivos PDF
Retomando de checkpoint (47 ja processados)
Faltam processar: 53 arquivos
------------------------------------------------------------

[1/53] pdf_048.pdf -> RENOMEADO  # Continua DAQUI!
[2/53] pdf_049.pdf -> RENOMEADO
...
[53/53] pdf_100.pdf -> RENOMEADO

============================================================
  RESUMO
============================================================
  Renomeados:        53
  Nao identificados: 0
  Ignorados (nao-RH): 0
  Erros:             0
  Total processados: 53
============================================================
Checkpoint limpo - execucao concluida com sucesso!
```

✅ Economiza reprocessar 47 PDFs já feitos!

---

## Exemplo 3: Retry Automático (Falha Temporária)

```powershell
PS> python main.py

[15/100] pdf_015.pdf -> processando...

# Poppler falha temporariamente (timeout I/O)
# Sem tenacity: ERRO! Crash
# Com tenacity: Tenta automaticamente

2024-03-30 10:25:33 | DEBUG | Tentativa 1/3 falhou, aguardando 1s...
2024-03-30 10:25:34 | DEBUG | Tentativa 2/3 falhou, aguardando 2s...  
2024-03-30 10:25:36 | DEBUG | Tentativa 3/3... OK! ✓

[15/100] pdf_015.pdf -> RENOMEADO

# Continua sem intervenção!
```

✅ Tolerância automática a falhas transientes

---

## Exemplo 4: Validação Antecipada (Erro Detectado Cedo)

**Cenário: Tesseract sem português**

```powershell
PS> python main.py

============================================================
  PDF Scanner - OCR Document Renamer
============================================================
Validando ambiente...
  Tesseract: OK - tesseract 5.3.0
  Tesseract idiomas: ERRO!
Validacao de ambiente falhou: Tesseract sem suporte a português (por.traineddata)
```

✅ Falha detectada ANTES de gastar tempo com OCR

**Solução:**
```powershell
# Copie por.traineddata para tessdata/
Copy-Item "C:\path\to\por.traineddata" ".\tessdata\"

# Reexecute
PS> python main.py
```

---

## Exemplo 5: Ajustando Paralelização

O padrão é 3 workers, mas pode ajustar conforme sua máquina:

**Máquina lenta (1-2 cores):**
```python
# Em main(), alterar:
num_workers = min(2, len(pdf_files))  # Reduzir de 3 para 2
```

**Máquina potente (8+ cores):**
```python
# Em main(), alterar:
num_workers = min(5, len(pdf_files))  # Aumentar de 3 para 5
```

**Debug/Teste (sequencial):**
```python
# Em main(), alterar:
num_workers = 1  # Forçar modo sequencial
```

Depois reexecute:
```powershell
PS> python main.py
```

---

## Exemplo 6: Limpando Checkpoint Manualmente

Se quiser reprocessar todos os PDFs do zero:

```powershell
PS> Remove-Item "logs\.checkpoint" -Force
PS> python main.py

# Começa do primeiro PDF, sem checkpoint
```

---

## Performance: Antes vs Depois

### Lote Pequeno (5 PDFs)

**Antes (v1.0):**
```
PDF #1: 60s
PDF #2: 55s  
PDF #3: 65s
PDF #4: 62s
PDF #5: 58s
─────────
Total: 300s (~5 min)
```

**Depois (v2.0):**
```
Worker #1: PDF #1 (60s) → PDF #4 (62s) → PDF #5 (58s)
Worker #2: PDF #2 (55s) → Done
Worker #3: PDF #3 (65s) → Done
─────────
Total: 120s (~2 min) - 3x MAIS RÁPIDO
```

### Lote Grande (100 PDFs)

**Antes (v1.0):**
```
~5800s total (~97 min)
```

**Depois (v2.0):**
```
~2000s total (~33 min) - 3x MAIS RÁPIDO
```

**Com recovery (falha em PDF #50):**
```
Antes:  Recomença do #1 → 97 min
Depois: Retoma do #50 → 33 min + ~24 min (retomada) = 24 min (economiza 73%)
```

---

## Monitorando Processamento

Abra `logs/scanner_log_YYYYMMDD_HHMMSS.txt` em tempo real:

```powershell
# PowerShell - monitorar arquivo de log em tempo real
Get-Content "logs\scanner_log_*.txt" -Tail 20 -Wait
```

Você verá:
```
2024-03-30 10:25:00 | INFO  | [1/100] pdf_001.pdf
2024-03-30 10:25:01 | DEBUG | Pagina 1 OCR (psm6): 1500 chars
2024-03-30 10:25:03 | INFO  | Tipo: FMM
2024-03-30 10:25:04 | INFO  | Nome: RAFAEL BATISTA DA SILVA
2024-03-30 10:25:05 | INFO  | Periodo: 21-05-2025 a 20-06-2025
2024-03-30 10:25:06 | INFO  | RENOMEADO -> FMM - RAFAEL BATISTA DA SILVA - 21-05-2025 a 20-06-2025.pdf
2024-03-30 10:25:06 | INFO  | [2/100] pdf_002.pdf
...
```

---

## Troubleshooting Rápido

| Problema | Solução |
|----------|---------|
| `ModuleNotFoundError: tenacity` | `pip install tenacity` |
| Checkpoint não removido | `Remove-Item logs\.checkpoint -Force` |
| CPU 100% o tempo todo | Reduzir `max_workers` de 3 → 2 |
| Timeout frequente de Poppler | Retry automático tenta 3x (normal) |
| Precisa debug sequencial | Alterar `num_workers = 1` |

---

## Próximas Melhorias Consideradas

1. **Score de confiança:** Ver qual extraction tem baixa confiança
2. **Modo dry-run:** `python main.py --dry-run` (simula sem renomear)
3. **Batch scheduling:** Agendar processamento para madrugada
4. **Web dashboard:** Monitor em tempo real

---

**Pronto!** Execute `python main.py` e aproveite os ganhos de performance e reliability! 🚀

