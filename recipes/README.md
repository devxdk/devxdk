# Build recipes

Per-component build/adopt recipes invoked by the build-runtimes leg jobs via
`recipes/leg.sh <component>-<goos>-<goarch>`. Each recipe builds every tracked
line for its (component, platform) pair, checks the archive layout against the
bundle contract, smoke-tests it on the target OS, and writes the archive +
`.meta.json` into `build/<leg>/` for the leg's artifact.

Recipes land with the phases that need them:

- **Phase 1** — `php.sh` (Windows repack of the official build + the pinned
  php_redis DLL + `templates/php.ini.windows`), `redis.sh` (MSYS2 source build).
- **Phase 3** — `php.sh` Unix path (static-php-cli + `templates/php.ini.unix`),
  `redis.sh` / `valkey.sh` Unix + MSYS2, `nginx.sh` (Unix source build). Adopt
  recipes (`python.sh`, `postgres.sh`) re-host upstream binaries by reference
  (self-hash + smoke, no rebuild).

Until a recipe exists, `recipes/leg.sh` fails a planned leg loudly rather than
publishing nothing. The pins the recipes verify against live in
`config/tracked-versions.toml` under `[pins]`.
