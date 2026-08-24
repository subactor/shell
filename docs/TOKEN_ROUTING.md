# Routing tokenów i orkiestracja NL → DSL

## Cel

Bridge ma zachować pełną konwersację dla użytkownika, ale nie używać pełnego transcriptu jako promptu wykonawczego. LLM pełni rolę ograniczonego parsera lub projektanta, natomiast dane, planowanie typowe, polityki i wykonanie pozostają lokalne.

## Warstwy

```text
L0 deterministic/cache
L1 local_4b structured parser
L2 cheap_remote structured repair/parser
L3 large_remote lub provider rozmowy
```

### L0 — zero tokenów

`CandidateRetriever` korzysta z normalizacji, szablonów fraz, ważonego overlapu tokenów i podobieństwa sekwencji. Exact lub template match jest walidowany względem definicji intentu. Wynik może trafić do cache jako walidowany `IntentIR`, nigdy jako wykonany side effect.

### L1/L2/L3 — krótki structured output

Parser otrzymuje tylko:

- tekst bieżącej wypowiedzi;
- shortlistę `top_k` intentów;
- wymagane i opcjonalne argumenty;
- krótkie reguły bezpieczeństwa;
- JSON Schema `IntentIR v1`.

Domyślny limit odpowiedzi parsera to 192 tokeny. Po odpowiedzi runtime ponownie sprawdza, czy intent pochodzi z shortlisty i czy argumenty należą do jego schematu.

## IntentIR, plan i receipt

Rozdzielenie reprezentacji jest celowe:

1. `IntentIR` — wybór celu i argumentów; może pochodzić z LLM.
2. `ExecutionPlan` — connector, operation, effect i mapowanie argumentów; zawsze generowane lokalnie.
3. `ExecutionReceipt` — krótki, typowany wynik lokalnego wykonania.

Model nie może umieścić w IntentIR pól `command`, `shell`, `connector`, `endpoint` ani wartości sekretu, ponieważ schema ma `additionalProperties: false`, a lokalna definicja intentu dodatkowo ogranicza dozwolone argumenty.

## WorkingState

`ContextBuilder` utrzymuje kompaktowy stan sesji. Provider rozmowy dostaje:

- cel i aktywny intent, o ile zostały ustalone;
- aktywne referencje i constraints;
- ostatni plan/receipt w postaci identyfikatora;
- informację o trasie i shortlistę kandydatów;
- ograniczoną liczbę ostatnich wiadomości.

Transcript w SQLite nie jest usuwany ani streszczany destrukcyjnie.

## Lokalne dane

Dane tekstowe i pliki nie są automatycznie wstawiane w całości. `select_relevant_text`:

1. dzieli tekst na fragmenty;
2. tokenizuje zapytanie i fragmenty;
3. punktuje overlap oraz kolejność;
4. zachowuje najwyżej ocenione fragmenty w kolejności źródłowej;
5. respektuje `max_chars` i `max_chunks`.

Jest to tani fallback algorytmiczny. Dla dużych baz lub kodu właściwym rozszerzeniem jest lokalny DOQL/DQL, BM25, indeks symboli albo embeddings, wystawiony jako bezpieczny named connector.

## Routing i progi

Konfiguracja:

```toml
[orchestration]
top_k = 5
min_candidate_score = 0.32
deterministic_threshold = 0.93
local_execute_threshold = 0.82
cheap_remote_threshold = 0.68
max_parser_output_tokens = 192
```

`confidence` nie pochodzi bezpośrednio od modelu. Router oblicza bazową wartość z wyniku retrievera i marginesu między kandydatami, a następnie może ją skalibrować historią udanych wykonań. Operacje wysokiego ryzyka nie są dopuszczane samym progiem probabilistycznym; przechodzą przez policy i approval.

## Cache

Klucz cache zależy od znormalizowanej wypowiedzi oraz fingerprintu katalogu intentów. Cache przechowuje tylko walidowany IntentIR i confidence. Wykonanie connectora nie jest odtwarzane z cache. Każdy plan ponownie przechodzi preflight i sprawdzenie aktualnego fingerprintu.

## Telemetria

`provider_usage` przechowuje:

```text
provider
model
purpose
input_tokens
cached_input_tokens
output_tokens
estimated
cost_usd
metadata
```

Gdy provider nie zwraca usage, runtime stosuje jawnie oznaczone oszacowanie zależne od długości tekstu. Koszt jest obliczany wyłącznie z wartości `*_cost_per_million` w lokalnym configu.

`routing_decisions` zapisuje trasę, intent, confidence, kandydatów i błędy parserów bez promptów i wartości sekretów. `router_feedback` pozwala później kalibrować routing na podstawie rzeczywistych sukcesów wykonania.

## Zalecane wdrożenie

1. Uruchomić `mode = "shadow"` i obserwować IntentIR bez lokalnego wykonania.
2. Włączyć `active` dla read-only i builtinów.
3. Dodać lokalny model 4B z JSON Schema.
4. Dodać tani zdalny fallback dopiero po zmierzeniu błędów lokalnego parsera.
5. Operacje write udostępniać przez nazwane connectory z plan/apply.
6. Duży LLM zostawić dla nowych procesów, nieznanych błędów i odpowiedzi konwersacyjnych.

## Co mierzyć

```text
schema-valid rate
intent accuracy
slot accuracy
unresolved rate
local route share
LLM-free route share
provider tokens per successful operation
execution success
user correction rate
false-safe / false-execute rate
```

Najważniejsza metryka ekonomiczna to nie koszt pojedynczego wywołania, lecz płatne tokeny na poprawnie zakończoną operację.
