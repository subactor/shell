# Integracja z aktualnym projektem Subactor

## Podstawa przeglądu

Aktualizacja została dopasowana do dostarczonego pliku `map.toon(2).yaml`. Mapa wskazuje ścieżki, symbole, importy i rozmiary elementów repozytorium. Nie zawiera pełnej treści wszystkich plików ani gwarantowanej semantyki ich runtime’u, dlatego bridge nie zgaduje dokładnych schematów process packów lub connector bindings. Integrację należy oprzeć na rzeczywistych plikach JSON/YAML z repozytorium nadrzędnego.

Z mapy wynika obecność następujących elementów:

- `config/intent-packs/*.v1.json` i `config/intent-packs/schema.v1.json`;
- `packages/intent-packs/src/phrase-matcher.mjs`;
- `resolveNlpUriFromPacks` w `packages/intent-packs/src/registry.mjs`;
- `packToNlpUriEntry` i `llmCatalogSliceFromPacks` w `derived-artifacts.mjs`;
- process packs z `process.v*.json`, `operations.v*.oql.json`, `expectations.v*.eql.json` i `recipe.v*.urirun.json`;
- `config/digital-twin/public-site-capability-inventory.doql.json` oraz warstwy DQL/DOQL;
- `packages/capability-preflight` i `scripts/capability-preflight.mjs`;
- `config/connector-capabilities`, `config/uri-process-connectors.json` i connector LAN;
- `packages/founder-cli/src/orchestrator-runner.mjs`;
- `scripts/lib/llm-cost-policy.mjs`.

To wspiera architekturę, w której bridge jest wejściem shell/ACP i kompilatorem krótkiego IntentIR, a istniejące komponenty Subactor pozostają źródłem katalogu, danych, preflightu i wykonania.

## Zalecany układ

```text
Shell / ACP client
  → Subactor Shell Bridge
      → phrase/template matcher
      → local 4B / cheap remote NL→IntentIR
      → local catalog + compiler + policy
      → Subactor Control albo nazwany connector
          → istniejący capability preflight
          → process pack / urirun / service
```

Bridge nie powinien kopiować implementacji DOQL/DQL, capability preflight ani urirun, jeżeli są już dostępne jako stabilna usługa lub connector. Powinien przekazywać im krótki, typowany plan.

## Intent packs

Można wskazać katalog lub pliki:

```toml
[orchestration]
intent_catalog_paths = [
  "/srv/subactor/config/intent-packs",
  "/etc/subactor/local-intents.json"
]
```

Loader bridge odczytuje pliki JSON rekursywnie i rozpoznaje między innymi typowe pola:

```text
id / intent_id / intentId / nlp_uri / uri
phrases / createPhrases / examples / utterances
required_args / requiredArgs / situationSchema.required
optional_args / optionalArgs
execution
connector + operation
recipe / urirun
```

Jeżeli format repozytorium zawiera pole `execution`, można mapować intent bezpośrednio na named connector. Jeżeli pack wskazuje jedynie recipe/urirun, należy dodać jawny adapter do rzeczywistego process-pack runnera. Sama mapa TOON nie wystarcza do bezpiecznego stworzenia tej translacji.

## Phrase matcher

Bridge ma własny lekki retriever Python do pracy samodzielnej. W repozytorium głównym preferowane są dwie opcje:

1. generować z istniejących intent packs zunifikowany katalog JSON przez `packToNlpUriEntry`/`llmCatalogSliceFromPacks` i ładować go do bridge;
2. wystawić istniejący `phrase-matcher.mjs` oraz `resolveNlpUriFromPacks` jako read-only connector, który zwraca shortlistę intentów.

W obu przypadkach wynik jest jeszcze lokalnie walidowany. Phrase matcher nie otrzymuje uprawnienia do wykonania.

## DOQL/DQL i dane lokalne

Dla prostych danych bridge ma lokalny wybór fragmentów. Dla digital twin i większych zbiorów rekomendowana ścieżka to named connector read-only, np.:

```toml
[connectors.subactor_query]
kind = "process"
command = ["/srv/subactor/bin/query-connector", "--json-stdin"]
allowed_operations = ["doql.query", "dql.query", "symbol.lookup"]
effect = "read"
```

Definicja intentu powinna mapować wyłącznie do wcześniej przygotowanego zapytania lub do ograniczonego DataQueryIR. Model nie powinien tworzyć dowolnego SQL, ścieżki pliku ani query endpointu.

## Process packs i urirun

Mapa wskazuje osobne pliki procesu, operations, expectations i recipes. Bridge traktuje je jako istniejący runtime poza swoją bazą kodu. Bezpieczna integracja:

1. IntentIR wybiera `pack_id` lub logiczny intent.
2. Lokalny adapter odnajduje dozwolony pack w registry.
3. Istniejący capability preflight sprawdza wymagania.
4. Adapter zwraca plan albo wykonuje go po approval.
5. Bridge zapisuje tylko krótki receipt i referencję do pełnego logu po stronie platformy.

Nie należy pozwalać LLM wskazywać dowolnej ścieżki `recipe.v*.urirun.json`.

## Capability preflight

Bridge wykonuje podstawowy preflight registry connectorów. Dla operacji platformowych należy dodatkowo wykorzystać istniejący `packages/capability-preflight` lub jego usługę. Dobry kontrakt connectora read-only:

```json
{
  "operation": "capability.preflight",
  "args": {
    "intent_id": "site.publish",
    "target_ref": "deployment://docs-prod"
  }
}
```

Wynik powinien być znormalizowany i krótki, np. `ready`, `missing_capabilities`, `state_version`, bez całego doctor payloadu.

## LLM cost policy i orchestrator runner

`llm-cost-policy.mjs` oraz `orchestrator-runner.mjs` mogą pozostać nadrzędną polityką platformy. Bridge zapisuje własne `routing_decisions` i `provider_usage`, a następnie może wystawić je jako input dla tej polityki. Należy unikać dwóch niezależnych komponentów, które oba samodzielnie podejmują ostateczną decyzję o wykonaniu. Zalecany podział:

- bridge decyduje, czy potrzebny jest parser/LLM i kompiluje lokalny plan;
- platformowy orchestrator decyduje o dostępności procesu i sposobie wykonania;
- ostateczna operacja write nadal wymaga policy/grant/approval.

## Zamknięta granica Subactor Control

Warstwa rozmowy jest instalowana obok Subactor Control, nie wewnątrz tej samej trasy MCP:

```text
Subactor Control
  cli.status
  cli.plan
  cli.execute

Subactor Shell Bridge
  chat / ACP
  IntentIR / plans / receipts
  data / Vault refs / metrics
```

Bridge przed wywołaniem Control sprawdza `tools/list`, a wynik musi odpowiadać allowliście z configu. Dodawanie `chat.*`, `data.*` lub `vault.*` do tego samego endpointu osłabiłoby tę granicę.

## Instalacja obok repozytorium

```bash
python -m venv ~/.local/share/subactor-shell/venv
~/.local/share/subactor-shell/venv/bin/pip install subactor_shell_bridge-0.2.0-py3-none-any.whl
~/.local/share/subactor-shell/venv/bin/subactor-shell init
```

Przykładowy config:

```toml
[orchestration]
mode = "shadow"
intent_catalog_paths = ["/srv/subactor/config/intent-packs"]

[control]
base_url = "http://127.0.0.1:8088"
allowed_tools = ["cli.status", "cli.plan", "cli.execute"]
bearer_ref = "file://~/.config/subactor-shell/control.token"
```

Najpierw zalecany jest `shadow`, porównanie IntentIR z aktualnym routingiem platformy i dopiero potem włączenie lokalnego wykonania read-only.
