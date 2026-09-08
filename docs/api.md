# API contract

The local manager uses JSON errors in the form `{"error":"..."}`. Browser writes require both a same-origin `Origin` or `Referer` and the `X-CSRF-Token` value returned by `GET /api/csrf` in the matching `sticker_csrf` cookie. When a bearer is configured, every API and asset request also requires `Authorization: Bearer <token>`; the UI keeps that token in memory only.

`GET /api/stickers?q=&page=1&page_size=50&include_deleted=0` returns `{items,total,page,page_size}`. `POST /api/stickers` accepts multipart `files` for images or ZIP files. `GET/PATCH/DELETE /api/stickers/{id}` reads, edits, or soft-deletes one item; `POST /api/stickers/{id}/restore` reverses a soft delete. `GET /assets/{id}` returns the original local image after host and bearer checks.

`GET/PATCH /api/settings` manages the usage switches and frequency values. `GET/PATCH /api/vision` manages provider/model/base URL and an API-key environment name; an optional `api_key` is stored only in a local mode-600 private file and is always returned masked. `POST /api/vision/test` requires `{ "confirm_cost": true }` and makes one real provider request with a built-in one-pixel image. It can incur provider cost.

`POST /api/tag` accepts `{ "ids": ["..."], "all_pending": false }`. `GET /api/jobs` reports queue progress and `POST /api/jobs/{id}/retry` explicitly retries a failed job. Auto-tagging on import is controlled by `auto_tag`; turning it off does not block manual tagging or explicit retry. `POST /api/backup` downloads a portable backup ZIP and `POST /api/restore` accepts a multipart `file` and validates every image and metadata item before a transaction.

The MCP server exposes exactly two tools. `express` returns a bounded JPEG preview by default, stable metadata, and a `sticker://{id}` resource URI; `include_image=false` avoids returning image bytes when the host charges for vision input. `sticker_library` supports `search`, `get`, `feedback`, `status`, and `manage`. MCP image results and resources are standard protocol values; whether an agent renders or sends them is a host capability, not a claim made by this server.
