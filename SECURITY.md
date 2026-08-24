# Security policy and threat model

## Chronione wartości

- klucze API providerów;
- token Vault;
- bearer Subactor Control i HTTP connectorów;
- wartości rozwiązane z `{{secret:ALIAS}}`;
- zmienne przekazane przez `connectors.*.env_refs`.

Mogą istnieć w pamięci procesu i w autoryzowanych żądaniach wychodzących, ale nie powinny trafić do SQLite, eksportów rozmów, artefaktów, promptów telemetrycznych, wyjątków, stdout ACP ani receiptów.

## Model nie jest źródłem uprawnień

LLM może wygenerować wyłącznie `IntentIR v1`. Nie może wybrać:

- komendy shell;
- ścieżki executable;
- connectora lub operation;
- URL endpointu;
- secret ref ani wartości sekretu;
- polityki approval.

Te elementy pochodzą z lokalnego katalogu intentów, konfiguracji connectorów i policy engine. JSON poprawny składniowo nadal jest traktowany jako niezaufany i przechodzi walidację semantyczną.

## Granice zaufania

1. Lokalny użytkownik oraz konto systemowe procesu są zaufane.
2. Pliki intent catalog i config connectorów są kodem/polityką operacyjną; ich modyfikacja wymaga takiej samej ochrony jak skryptów wdrożeniowych.
3. Provider endpoint otrzymuje tylko kontekst przygotowany przez router; jawne dane mogą opuścić host zgodnie z configiem.
4. Vault jest źródłem wartości sekretów; SQLite przechowuje tylko referencje.
5. Subactor Control pozostaje osobnym endpointem z dokładną allowlistą.
6. Connector process/HTTP jest zaufanym adapterem, ale jego stdout/stderr lub body odpowiedzi nadal są ograniczane i redagowane.
7. ACP powinien być uruchamiany wyłącznie dla zaufanego klienta lokalnego. Klient może grantować wcześniej związany alias i umieścić jego placeholder w bieżącym promptcie.

## Ochrona wykonania

- process connector używa `create_subprocess_exec`, nie `shell=True`;
- executable jest stały w configu i musi mieć ścieżkę absolutną;
- model przekazuje tylko argumenty przewidziane przez `argument_map`;
- connector i operation podlegają allowliście;
- effect planu nie może przekroczyć limitu connectora;
- operacje write wymagają dokładnego `EXECUTE`;
- apply weryfikuje plan hash i fingerprint aktualnego stanu/katalogu/registry;
- destructive jest domyślnie wyłączone;
- wykonanie zatrzymuje się po pierwszym nieudanym kroku;
- stdout/stderr/body są limitowane rozmiarem.

## Sekrety i prompt injection

Tylko placeholder wpisany bezpośrednio w bieżącej wiadomości użytkownika może zużyć jednorazowy grant. Placeholder znaleziony w:

- `{{data:...}}`;
- artefakcie;
- załączniku;
- embedded resource ACP;
- resource link;

pozostaje zwykłym tekstem i nie uruchamia resolvera sekretu.

Lokalny fast path jest wykonywany przed rozwinięciem sekretów, więc nie odczytuje wartości i nie konsumuje grantu. Odpowiedź providera przechodzi przez strumieniowy redaktor dokładnych wartości, także gdy wartość jest rozcięta pomiędzy fragmenty streamu.

## Dane wysyłane zdalnie

`{{data:NAME}}` i artefakty są jawne. Runtime ogranicza i wybiera fragmenty, ale nie klasyfikuje automatycznie poufności. Dane objęte ograniczeniem lokalności powinny być obsługiwane przez lokalny parser/retriever/connector, a zdalne profile nie powinny otrzymywać ich treści.

## Znane ograniczenia

- redakcja exact-value nie zastępuje DLP i może nie wykryć przekształconej, zakodowanej lub częściowo ujawnionej wartości;
- lokalny lexical chunk selector nie rozumie wszystkich zależności semantycznych;
- adapter HTTP ufa hostname z configu i nie implementuje osobnej ochrony DNS rebinding/egress firewall;
- `inherit_env = true` świadomie przekazuje connectorowi całe środowisko rodzica i powinno być używane tylko dla zaufanego adaptera; domyślnie runtime stosuje minimalne `pass_env`;
- mapa TOON projektu nie jest kontraktem wykonawczym; connector do process packs musi walidować rzeczywiste schematy repozytorium;
- integralność configu i katalogu intentów zależy od uprawnień systemu plików i procesu wdrożeniowego.

## Raportowanie

Nie dołączaj prawdziwych sekretów. Podaj minimalny przypadek z wartościami mock, wersję, system operacyjny, config po redakcji oraz oczekiwany i rzeczywisty routing/plan.
