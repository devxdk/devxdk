# Build recipes

Per-component build/adopt recipes invoked by the build-runtimes leg jobs via
`scripts/run_leg.py <component>-<goos>-<goarch>`. The runner invokes
`recipes/leg.sh` once per planned family, verifies its metadata and all member
bytes, and collects only verified results into `build/<leg>/handoff/`. A failed
family does not prevent later families from building. `outcomes.json` records
each result; the job fails after uploading the verified handoff.

Recipes:

- `php.sh` (Windows repack of the official build + the pinned
  php_redis DLL + `templates/php.ini.windows`), `redis.sh` (MSYS2 source build).
- `php.sh` Unix path (static-php-cli + `templates/php.ini.unix`),
  `redis.sh` / `valkey.sh` Unix + MSYS2, `nginx.sh` (Unix source build). Adopt
  recipes (`python.sh`, `postgres.sh`) re-host upstream binaries by reference
  (self-hash + smoke, no rebuild).
- `mariadb.sh` builds native macOS archives from the official source digest,
  checks Mach-O dependencies for host paths, then initializes and queries InnoDB.

Python proofs include native extensions and a new venv with pip. PostgreSQL proofs
initialize a cluster and execute SQL. PHP must load every baseline extension
without startup warnings. A configured target is not supported until these
native proofs and authenticated publication succeed.

Until a recipe exists, `recipes/leg.sh` fails a planned leg loudly rather than
publishing nothing. The pins the recipes verify against live in
`config/tracked-versions.toml` under `[pins]`.
