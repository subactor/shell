# Minimalny przykład dla KV v2 zamontowanego jako "secret".
# Ogranicz prefiks do konkretnego konta/środowiska.
path "secret/data/subactor/*" {
  capabilities = ["create", "read", "update", "patch"]
}

path "secret/metadata/subactor/*" {
  capabilities = ["read", "list"]
}
