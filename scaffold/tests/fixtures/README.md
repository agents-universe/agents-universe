# Test fixtures

Upload assets referenced by generated specs live here. `playwright test` runs
with `tests/` as the working directory, so a fixture is addressed as
`fixtures/<name>` — never with `__dirname` (the specs are ESM, where it does
not exist).

Prefer generating the payload inline in the spec (`content` / `content_base64`
on the case's `uploads` entry) when it is small and synthetic: the test then
carries its own data and cannot rot when a file is moved. Put a file here when
it is large (over ~512KB), shared by several cases, or must be byte-identical
to a real-world sample.

Fixtures are inputs, not results — Playwright writes screenshots, videos and
traces to `test-results/`.
