# Changelog

## 0.2.0 — 2026-08-24

### Dodano

- kaskadowy router deterministic/cache → local 4B → cheap remote → large/chat provider;
- `IntentIR v1` z JSON Schema i semantyczną walidacją shortlisty/argumentów;
- lokalny katalog intentów oraz retriever exact/template/lexical;
- `WorkingState` i ograniczony builder kontekstu zamiast pełnego transcriptu;
- lokalny wybór fragmentów danych i artefaktów;
- deterministyczny `ExecutionPlan`, plan hash i fingerprint stanu;
- named connector registry: builtin, Subactor Control, stały process argv i HTTP;
- policy engine oraz dokładne `EXECUTE` dla operacji zmieniających stan;
- krótkie `ExecutionReceipt`;
- tabele SQLite dla stanu, routingu, usage, planów, receiptów, feedbacku i cache;
- CLI: `plans`, `receipts`, `metrics`, `catalog`, `connectors`;
- REPL: `/plans`, `/plan`, `/apply`, `/receipts`, `/receipt`, `/route`, `/metrics`, `/catalog`, `/connectors`;
- ACP extensions do katalogu, connectorów, trasy, metryk, planów i receiptów;
- usage input/cached/output oraz lokalne szacowanie kosztu według configu;
- profile OpenAI-compatible bez auth dla lokalnych serwerów i native JSON Schema structured output;
- przykładowy intent catalog, process connector i schematy.

### Zmieniono

- provider rozmowy otrzymuje tylko ograniczoną historię i kompaktowy kontekst;
- `{{data:...}}` oraz załączniki nie są automatycznie rozwijane w całości;
- lokalne intenty są wykonywane przed rozwiązaniem sekretów;
- eksport sesji zawiera routing, usage, plany i receipty bez wartości sekretów;
- wersja projektu i domyślnego configu podniesiona do 0.2.0.

### Kompatybilność

- istniejące sesje, wiadomości, artefakty, dane i bindingi sekretów 0.1 pozostają zachowane;
- schema SQLite jest rozszerzana migracyjnie przy otwarciu Store;
- dotychczasowe komendy chat/data/vault/sessions/export/doctor/acp-agent pozostają dostępne.

### Weryfikacja wydania

- testy dotychczasowych funkcji bezpieczeństwa i providerów;
- test ograniczonego kontekstu przy zachowaniu pełnego transcriptu;
- test lokalnej selekcji fragmentów;
- test fast path bez wywołania LLM;
- test lokalnego parsera structured IntentIR;
- test plan/apply process connectora;
- test JSON Schema i usage dla OpenAI-compatible;
- test rozszerzeń ACP.
