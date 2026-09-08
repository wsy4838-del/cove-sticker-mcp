# Verification record

This record describes the package verification performed locally. It is an
acceptance record, not a claim that the package has been published.

## Wheel build and clean installation

Build the wheel without installing the development extras into the runtime
environment:

```bash
python -m build --wheel --no-isolation --outdir dist
python -m venv /private/tmp/sticker-mcp-clean-venv
/private/tmp/sticker-mcp-clean-venv/bin/python -m pip install dist/sticker_mcp-0.1.0.dev0-py3-none-any.whl
/private/tmp/sticker-mcp-clean-venv/bin/python -m pip check
```

The observed clean environment used for this record was:

```text
Python 3.13.12
sticker-mcp 0.1.0.dev0
mcp 2.0.0
starlette 0.52.1
python-multipart 0.0.32
uvicorn 0.52.4
filelock 3.32.5
Pillow 12.3.0
httpx 0.28.1
pip check: No broken requirements found.
```

The existing shared test environment had `starlette 1.3.1`; it was not used
as the dependency-resolution environment for this wheel record. The package
constraint is `starlette>=0.46,<1`, and the clean install resolved the
compatible `0.52.1` release.

The wheel was installed with all runtime dependencies resolved. An earlier
smoke test used `uv pip install --no-deps` plus an external dependency path;
that test is not used as the clean-install evidence above.

## Protocol and behavior checks

The repository test suite completed with `32 passed`. `ruff check .`,
`compileall`, and `git diff --check` also passed. A client launched from the
cleanly installed wheel completed MCP `initialize` and `tools/list`, returning
`express` and `sticker_library`; stdout remained protocol-only and logs went to
stderr. The browser acceptance run separately covered bearer rejection,
import/edit/manual-field preservation, standard MCP image output, original
resource reading, backup restore, and a 390px viewport.

`POST /api/vision/test` was not invoked against a live provider during this
verification because it intentionally performs a real, potentially paid model
request. Provider payload and timeout behavior was tested with HTTP mocks.
