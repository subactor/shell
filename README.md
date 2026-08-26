# Subactor Shell 0.2.2

Subactor Shell jest trwałą warstwą rozmowy i orkiestracji dla terminala oraz klientów ACP. Wersja 0.2.2 integruje pytania operacyjne z zarządzanym CLI Subactora, poprawia obsługę kończenia sesji i nie przejmuje nazwy Founder Chat.

```text
polecenie użytkownika
  → exact/template/phrase match (0 tokenów)
  → lokalny lub tani parser NL → IntentIR v1
  → lokalna walidacja JSON Schema
  → deterministyczny ExecutionPlan
  → policy + capability/connector preflight
  → nazwany connector
  → krótki ExecutionReceipt
  → duży LLM tylko przy niepewności lub zadaniu konwersacyjnym
```

Pełny transcript nadal jest zapisywany w SQLite, ale provider rozmowy dostaje tylko ograniczony `WorkingState`, kilka ostatnich wiadomości, krótką informację o trasie oraz lokalnie wybrane fragmenty danych i artefaktów.

## Najważniejsze właściwości

- lokalny fast path bez LLM dla znanych poleceń;
- typowany i walidowany `IntentIR v1` zamiast swobodnego planowania w prozie;
- routing: deterministic/cache → local 4B → cheap remote → large/chat provider;
- `ExecutionPlan` tworzony wyłącznie przez lokalny kompilator;
- nazwane connectory `builtin`, Subactor Control, process oraz HTTP;
- brak `shell=True` i brak możliwości wskazania przez model dowolnej komendy;
- plan hash, fingerprint stanu i jawne `EXECUTE` dla operacji zmieniających stan;
- `ExecutionReceipt` zamiast przekazywania pełnych logów między modelami;
- telemetria tokenów, cached input, szacowanego kosztu i udziału tras bez LLM;
- trwałe sesje, jawne dane, artefakty oraz referencje Vault;
- ACP v1 po `stdin/stdout`, wraz z rozszerzeniami katalogu, planów, receiptów i metryk;
- migracja istniejącej bazy 0.1 bez usuwania sesji ani wiadomości.

## Instalacja

Pakiet instaluje wyłącznie polecenie `subactor-shell`. Nazwa `subactor` jest
zarezerwowana dla Founder Chat dostarczanego przez Platformę, dzięki czemu
`subactor chat` zachowuje swój interfejs, pełną diagnostykę i kontrakt sesji.
Powłokę z trwałym stanem, Vault, lokalnym routingiem i ACP uruchamia się
jawnie przez `subactor-shell chat`.

Z wheel:

```bash
python -m venv .venv
. .venv/bin/activate
pip install ./subactor_shell_bridge-0.2.0-py3-none-any.whl
subactor-shell init
```

Ze źródeł:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e .
subactor-shell init
```

Domyślne lokalizacje:

```text
~/.config/subactor-shell/config.toml
~/.local/share/subactor-shell/subactor-shell.sqlite3
~/.local/share/subactor-shell/artifacts/
```

Katalog danych otrzymuje tryb `0700`, a config, SQLite i artefakty `0600`, o ile system plików wspiera te tryby.

## Szybki start

```bash
subactor-shell chat
```

Przykładowe polecenia w REPL:

```text
pokaż sesje
pokaż zużycie tokenów
/status
/plans
/receipts
/catalog
/connectors
/route
```

Jedna wiadomość bez REPL:

```bash
subactor-shell one 'pokaż sesje'
```

Przy znanym intencie read-only wynik może zostać wykonany lokalnie bez wywołania providera rozmowy.

## Routing modeli

Minimalna konfiguracja lokalnego parsera OpenAI-compatible:

```toml
[orchestration]
enabled = true
mode = "active"
local_parser_provider = "local_4b"
local_parser_model = "local-4b-instruct"
cheap_parser_provider = ""
large_provider = ""
top_k = 5
max_parser_output_tokens = 192

[providers.local_4b]
kind = "openai_compat"
base_url = "http://127.0.0.1:8000/v1"
endpoint = "/chat/completions"
auth_required = false
api_key_ref = ""
model = "local-4b-instruct"
max_output_tokens = 192
structured_mode = "json_schema"
```

Tani i duży fallback można dodać jako kolejne profile:

```toml
[orchestration]
local_parser_provider = "local_4b"
cheap_parser_provider = "budget_remote"
large_provider = "planner_remote"

[providers.budget_remote]
kind = "openai_compat"
base_url = "https://provider.example/v1"
endpoint = "/chat/completions"
api_key_ref = "env://BUDGET_LLM_API_KEY"
auth_required = true
model = "budget-model"
max_output_tokens = 192
structured_mode = "json_schema"
input_cost_per_million = 0.0
cached_input_cost_per_million = 0.0
output_cost_per_million = 0.0

[providers.planner_remote]
kind = "openai_compat"
base_url = "https://provider.example/v1"
endpoint = "/chat/completions"
api_key_ref = "env://PLANNER_LLM_API_KEY"
auth_required = true
model = "large-planner"
max_output_tokens = 800
structured_mode = "json_schema"
```

Stawki w configu są wyłącznie danymi użytkownika do lokalnego szacowania kosztu. Projekt nie pobiera automatycznie cenników.

Tryby orkiestracji:

- `active` — wykonuje poprawne lokalne plany;
- `shadow` — zapisuje routing i IntentIR, ale odpowiedź nadal prowadzi provider rozmowy;
- `off` — zachowanie konwersacyjne bez DSL.

## IntentIR v1

Model parsera może zwrócić tylko obiekt zgodny z `schemas/intent-ir.v1.schema.json`, na przykład:

```json
{
  "v": 1,
  "intent_id": "project.deploy",
  "mode": "plan",
  "args": {
    "project_ref": "project://docs",
    "environment": "prod"
  },
  "requirements": ["verify_tls"],
  "constraints": ["no_secret_export"],
  "unresolved": []
}
```

Model nie wybiera komendy, ścieżki wykonywalnej, URL, connectora ani secret ref. Te elementy pochodzą z lokalnego katalogu intentów i konfiguracji connectorów.

## Katalog intentów

Wbudowane intenty obejmują pomoc, sesje, dane, bindingi sekretów, metryki oraz `cli.status`/`cli.plan` istniejącego Subactor Control.

Dodatkowe katalogi wskazuje się w configu:

```toml
[orchestration]
intent_catalog_paths = ["./intent-catalog.v1.json"]
```

Przykład znajduje się w `examples/intent-catalog.v1.json`. Loader akceptuje własny format `{"intents": [...]}` oraz kilka typowych nazw pól spotykanych w intent packach, ale dokładna integracja z repozytorium nadrzędnym wymaga rzeczywistych plików JSON, nie samej mapy symboli.

## Named connectors

### Process connector

```toml
[connectors.project_ops]
kind = "process"
command = ["/opt/subactor/bin/project-connector", "--json-stdin"]
allowed_operations = ["project.inspect", "project.apply"]
effect = "external_write"
inherit_env = false
pass_env = ["PATH", "LANG", "LC_ALL", "TZ"]
timeout_seconds = 30.0
output_limit_bytes = 65536

[connectors.project_ops.env_refs]
PROJECT_API_TOKEN = "vault://secret/subactor/project#token"
```

Pierwszy element `command` musi być ścieżką absolutną. Runtime uruchamia stałe argv przez `create_subprocess_exec`, bez powłoki. Domyślnie nie dziedziczy całego środowiska procesu; przepuszcza tylko nazwy z `pass_env` oraz jawne `env_refs`. Connector dostaje JSON przez `stdin`:

```json
{
  "plan_id": "plan_...",
  "plan_hash": "...",
  "session_id": "...",
  "intent_id": "project.apply",
  "operation": "project.apply",
  "args": {}
}
```

Minimalny przykład implementacji: `examples/process-connector.py`.

### HTTP connector

```toml
[connectors.project_http]
kind = "http"
base_url = "https://connector.internal"
path = "/v1/execute"
method = "POST"
bearer_ref = "file://~/.config/subactor-shell/project-http.token"
allowed_operations = ["project.inspect", "project.apply"]
effect = "external_write"
```

### Subactor Control

`subactor_control` pozostaje specjalnym connectorom z dokładną allowlistą:

```toml
[control]
allowed_tools = ["cli.status", "cli.plan", "cli.execute"]
```

Bridge wykonuje `tools/list` przed wywołaniem i odrzuca endpoint, który reklamuje inny zestaw narzędzi. `cli.execute` wymaga zaakceptowanego planu.

## Plan i apply

Operacja read-only może wykonać się automatycznie. Operacja zmieniająca stan zapisuje plan:

```bash
subactor-shell plans list
subactor-shell plans show PLAN_ID
subactor-shell plans apply PLAN_ID --confirm EXECUTE
subactor-shell receipts list
subactor-shell receipts show RECEIPT_ID
```

Przed apply sprawdzane są:

1. status planu;
2. `plan_hash`;
3. dokładne `EXECUTE` dla zmian stanu;
4. aktualny fingerprint SQLite, katalogu intentów i registry connectorów;
5. allowlista connectora i operation;
6. lokalna policy.

## Ograniczanie kontekstu

Pełna historia jest przechowywana, lecz nie jest ponownie wysyłana przy każdej turze. Limity ustawia sekcja:

```toml
[context]
recent_messages = 6
max_history_chars = 12000
max_message_chars = 4000
max_data_chars = 6000
max_attachment_prompt_chars = 8000
artifact_chunk_chars = 1800
max_artifact_chunks = 4
max_embedded_context_chars = 8000
max_route_context_chars = 4000
```

`{{data:NAME}}` i załączniki są dzielone lokalnie na fragmenty i wybierane leksykalnie względem bieżącego polecenia. Model otrzymuje tylko wynik mieszczący się w budżecie. To nie jest pełny silnik semantyczny; dla rozbudowanego repozytorium należy podłączyć istniejące DOQL/DQL lub własny retriever jako nazwany connector.

## Dane i artefakty

```bash
subactor-shell data set ENVIRONMENT staging
subactor-shell data put SPEC ./specification.md
subactor-shell data list
```

W rozmowie:

```text
Przeanalizuj ustawienia {{data:ENVIRONMENT}} oraz sekcję deployment w {{data:SPEC}}.
```

Jawne dane mogą zostać wysłane do providera po lokalnym wyborze fragmentów. Nie należy zapisywać w tej warstwie sekretów.

## Vault i jednorazowe granty

Binding zapisuje wyłącznie referencję:

```bash
subactor-shell vault bind DB_PASSWORD 'vault://secret/subactor/prod/database#password'
```

Zapis wartości do Vault KV v2 pobiera ją bez echa:

```bash
subactor-shell vault put DB_PASSWORD 'vault://secret/subactor/prod/database#password'
```

W REPL:

```text
/vault grant DB_PASSWORD
Sprawdź format {{secret:DB_PASSWORD}}, ale jej nie powtarzaj.
```

Grant jest jednorazowy i przechowywany wyłącznie w pamięci procesu. Placeholder w zapisanych danych, pliku lub embedded resource ACP nie może sam zużyć grantu. Lokalne fast path nie odczytuje sekretu i nie konsumuje grantu.

## Metryki

```bash
subactor-shell metrics --json
subactor-shell metrics --session SESSION_ID --json
```

Wynik obejmuje:

- liczbę wywołań providerów;
- input, cached input i output tokens;
- wywołania z usage oszacowanym lokalnie;
- koszt według stawek wpisanych w configu;
- rozkład tras;
- udział tras `deterministic` i `cache` bez LLM.

## ACP

```bash
subactor-shell acp-agent
```

Oprócz ACP v1 (`initialize`, `session/new`, `session/load`, `session/prompt`, `session/cancel`) agent obsługuje rozszerzenia:

```text
subactor/data/set
subactor/data/list
subactor/secret/bind
subactor/secret/grant
subactor/secret/list
subactor/catalog/list
subactor/connectors/list
subactor/route/get
subactor/metrics/get
subactor/plan/list
subactor/plan/get
subactor/plan/apply
subactor/receipt/list
subactor/receipt/get
```

`subactor/plan/apply` używa tej samej walidacji i wymaga pola `confirmation: "EXECUTE"` dla zmian stanu. `stdout` procesu ACP jest zarezerwowany dla jednoliniowych komunikatów JSON-RPC.

## Migracja z 0.1

Po wskazaniu istniejącego `--data-dir` Store automatycznie dodaje tabele:

```text
session_state
routing_decisions
provider_usage
execution_plans
execution_receipts
router_feedback
context_cache
```

Istniejące `sessions`, `messages`, `artifacts`, jawne dane i bindingi sekretów pozostają zachowane. Przed migracją produkcyjną zalecana jest kopia pliku SQLite.

## Testy

```bash
python -m pip install -e '.[dev]'
pytest -q
```

Pakiet wydaniowy 0.2.0 został sprawdzony testami jednostkowymi i integracyjnymi oraz instalacją wheel do izolowanego katalogu pakietów, uruchomioną poza drzewem źródeł. Integracje z prawdziwymi kontami Vault/LLM/Subactor należy dodatkowo sprawdzić w docelowym środowisku.

Więcej szczegółów:

- `docs/TOKEN_ROUTING.md` — routing, WorkingState i budżety;
- `docs/INTEGRATION.md` — dopasowanie do aktualnej mapy repozytorium Subactor;
- `SECURITY.md` — granice zaufania i model zagrożeń;
- `CHANGELOG.md` — zakres wersji 0.2.0.
