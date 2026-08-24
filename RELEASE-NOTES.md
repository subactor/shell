# Subactor Shell Bridge 0.2.0 — release notes

Data wydania: 2026-08-24

## Cel wydania

Wersja 0.2.0 wdraża oszczędzanie tokenów przez lokalne rozpoznanie intentu, ograniczony kontekst i deterministyczne wykonanie istniejących skryptów/usług/connectorów. Duży LLM jest fallbackiem konwersacyjnym lub plannerem, a nie bezpośrednim wykonawcą.

## Najważniejsze zmiany

- `IntentIR v1` i JSON Schema;
- deterministic/cache fast path bez LLM;
- lokalny parser 4B, tani remote parser i duży fallback;
- `WorkingState` oraz budżety historii, danych, artefaktów i embedded context;
- lokalny lexical chunk selection;
- lokalny kompilator `ExecutionPlan`;
- named connectors z allowlistą operation;
- process connector bez `shell=True` i bez pełnego dziedziczenia środowiska domyślnie;
- plan hash, state fingerprint, policy i dokładne `EXECUTE`;
- krótkie `ExecutionReceipt`;
- telemetria usage/kosztu/routingu;
- nowe komendy CLI/REPL i rozszerzenia ACP;
- migracja bazy danych 0.1 przez dodanie nowych tabel.

## Dopasowanie do dostarczonej mapy projektu

`docs/INTEGRATION.md` opisuje współpracę z elementami widocznymi w `map.toon(2).yaml`: intent packs, phrase matcher, derived LLM catalog, process packs, DOQL/DQL, capability preflight, connector registry, `llm-cost-policy.mjs` i `orchestrator-runner.mjs`.

Mapa jest indeksem symboli, nie pełnym kontraktem runtime. Wydanie nie wprowadza zgadywanego bezpośredniego wykonawcy process packów; wymaga on named connectora opartego na rzeczywistych schematach repozytorium.

## Weryfikacja

- 29 testów jednostkowych i integracyjnych: wszystkie przeszły;
- kompilacja wszystkich modułów Python;
- budowa wheel 0.2.0;
- instalacja wheel do izolowanego katalogu pakietów poza drzewem źródeł;
- smoke test CLI: version, init, one, sessions, metrics, catalog, connectors;
- smoke test ACP stdio: initialize, session/new, catalog/list, metrics/get, data/list;
- test lokalnego parsera structured output → IntentIR → builtin bez streamu dużego LLM;
- test write plan/apply z dokładnym `EXECUTE` i stałym process connectorem;
- test, że process connector domyślnie nie dziedziczy niezadeklarowanej zmiennej środowiskowej;
- test migracji tabel 0.1 bez utraty sesji, wiadomości, danych i secret refs.

## Niewykonane testy środowiskowe

Nie użyto prawdziwych poświadczeń ani produkcyjnych endpointów Vault, OpenAI-compatible, Anthropic, Z.ai, Subactor Control, process packs lub prywatnych connectorów. Integracje protokołów zostały sprawdzone testami kontrolowanymi i mockami; docelowe endpointy wymagają osobnego testu wdrożeniowego.
