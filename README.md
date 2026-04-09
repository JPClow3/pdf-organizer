# PDF Scanner OCR v2.1

Sistema de OCR para processar PDFs na pasta de scanner, classificar o tipo de documento e renomear no padrao:

`TIPO - NOME - DATA.pdf`

## Visao Geral

- OCR com Tesseract + Poppler
- Classificacao por assinaturas e score de confianca
- Gate de confianca com fila de revisao
- Quarentena para erros permanentes
- Processamento em lote com paralelismo
- Suporte a documentos em portugues

## Pre-requisitos

- Windows 10+
- Python 3.8+
- Tesseract OCR instalado
- Poppler instalado (`pdftoppm` no PATH ou detectavel)
- Permissao de leitura e escrita na pasta scanner

## Instalacao

```bash
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Configuracao

O aplicativo usa `config.ini` na raiz do projeto.

Exemplo compativel com o codigo atual:

```ini
[paths]
scanner_dir = G:/RH/EQUIPE RH/ARQUIVO/SCANNER

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
gen = 70
```

## Uso

Execucao unica:

```bash
python main.py
```

Modo continuo:

```bash
python main.py --watch
```

Intervalo customizado no modo watch:

```bash
python main.py --watch --watch-interval 10
```

Validacao local:

```bash
python final_validation_tests.py
```

## Estrutura Atual

```text
.
|- main.py
|- config.ini
|- README.md
|- requirements.txt
|- final_validation_tests.py
|- models/
|  |- custom_models.json
|- sample/
|- tessdata/
|  |- por.traineddata
```

## Observacoes Operacionais

- O processamento normal olha apenas a raiz de `scanner_dir`.
- O sistema pode criar `_REVISAO` e `_QUARENTENA` automaticamente.
- `monitor_confidence.py` e os templates MBV sao opcionais.
- Se `monitor_confidence.py` nao existir, a telemetria de baixa confianca fica desabilitada e o aplicativo registra aviso no log.
- Se os arquivos em `templates/` nao existirem, a extracao MBV continua em modo degradado por ROI, com possivel perda de acuracia.
- O script `final_validation_tests.py` agora falha com codigo diferente de zero quando qualquer check obrigatorio falha.

## Troubleshooting

`Tesseract not found`

- Verifique a instalacao do Tesseract.
- Garanta que o executavel esteja acessivel no ambiente.

`pdftoppm not found`

- Instale Poppler.
- Garanta que o binario esteja no PATH.

Muitos arquivos indo para `_REVISAO`

- Revise os thresholds em `[confidence]` no `config.ini`.
- Avalie qualidade do scan, DPI, nitidez e contraste.

Arquivo em `_QUARENTENA`

- Pode estar corrompido, incompleto ou em uso por outro processo.
- Reprocesse manualmente apos validar o PDF.

## Status

- Versao: 2.1
- Estado da documentacao: alinhada com a arvore atual do repositorio
- Ultima atualizacao do README: 2026-04-09
