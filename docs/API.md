# ERP-Rent REST API (v1)

A JSON API for programmatic quote management, mounted at `/api/v1`.

- [Authentication](#authentication)
- [Conventions](#conventions)
- [Quotes](#quotes)
  - [List quotes](#list-quotes)
  - [Get a quote](#get-a-quote)
  - [Create a quote](#create-a-quote)
  - [Update a quote](#update-a-quote)
- [Quote lines](#quote-lines)
  - [Add a line](#add-a-line)
  - [Update a line](#update-a-line)
  - [Delete a line](#delete-a-line)
  - [Reorder lines](#reorder-lines)
- [Items](#items)
  - [List / search items](#list--search-items)
- [Objects](#objects)
  - [Quote object](#quote-object)
  - [Line object](#line-object)
- [Error reference](#error-reference)

---

## Authentication

The API uses static bearer tokens. Configure one or more tokens via the
`API_TOKENS` environment variable (comma-separated):

```env
API_TOKENS=my-secret-token,another-token
```

Every request must include the header:

```
Authorization: Bearer <token>
```

| Condition | Response |
|---|---|
| `API_TOKENS` not set | `503` — API is disabled |
| Missing / invalid token | `401` |

> **Note:** Treat tokens like passwords. Use HTTPS in production and rotate
> tokens by updating `API_TOKENS` and restarting the app.

## Conventions

- **Base URL:** `/api/v1`
- **Content type:** request bodies are JSON (`Content-Type: application/json`).
- **Dates:** `YYYY-MM-DD` strings. Empty string or `null` clears a date.
- **Prices:** floats in EUR per unit per day. Whether values are gross
  (brutto) or net is controlled per quote via `prices_are_net`
  (default: gross).
- **Success responses:**
  ```json
  { "ok": true, ... }
  ```
- **Error responses:**
  ```json
  { "ok": false, "error": "message" }
  ```
- **Warnings:** mutating quote endpoints return the full updated
  [quote object](#quote-object) plus a `warnings` array with non-fatal
  problems (e.g. availability conflicts). Warnings do **not** prevent the
  change from being saved.
- **Editing rules:** only quotes in status `draft` can be modified. Mutations
  on `finalized` / `performed` / `paid` quotes return `409`. Status
  transitions themselves are not part of this API — use the admin UI.

---

## Quotes

### List quotes

```
GET /api/v1/quotes
```

Query parameters:

| Parameter | Type | Description |
|---|---|---|
| `status` | string | Filter by status: `draft`, `finalized`, `performed`, `paid` |
| `customer` | string | Case-insensitive substring match on the customer name |
| `limit` | int | Page size, default `100`, max `500` |
| `offset` | int | Pagination offset, default `0` |

Quotes are ordered by creation date, newest first.

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://localhost:5000/api/v1/quotes?status=draft&customer=meyer&limit=20"
```

Response (`quotes` contains [quote objects](#quote-object) *without* lines):

```json
{
  "ok": true,
  "total": 42,
  "quotes": [ { "id": 3, "customer_name": "Meyer GmbH", "...": "..." } ]
}
```

### Get a quote

```
GET /api/v1/quotes/<id>
```

Returns the full [quote object](#quote-object) including `lines`.

```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:5000/api/v1/quotes/3
```

### Create a quote

```
POST /api/v1/quotes
```

Creates a new quote in status `draft` and assigns a reference number.

| Field | Type | Required | Description |
|---|---|---|---|
| `customer_name` | string | **yes** | Customer name |
| `start_date` | date | no | Rental start (`YYYY-MM-DD`) |
| `end_date` | date | no | Rental end; must be ≥ `start_date` |
| `rental_days_override` | int | no | Override the date-based billed days |
| `recipient_lines` | string | no | Invoice address block (multiline) |
| `notes` | string | no | Internal notes (never printed) |
| `public_notes` | string | no | Notes printed on PDFs |
| `prices_are_net` | bool | no | Treat stored prices as net (default `false` = gross) |
| `discount_percent` | float | no | Discount in percent (0–100) |
| `discount_label` | string | no | Label shown next to the discount |

> Items can only be added once `start_date` **and** `end_date` are set
> (needed for availability and supplier sourcing).

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"customer_name": "Meyer GmbH", "start_date": "2026-09-04", "end_date": "2026-09-06"}' \
  http://localhost:5000/api/v1/quotes
```

Returns `201` with the full quote object.

### Update a quote

```
PATCH /api/v1/quotes/<id>
```

Accepts the same fields as [Create a quote](#create-a-quote). Only fields
present in the payload are changed. Setting both dates recalculates
`rental_days`; `rental_days_override: null` removes an override.

```bash
curl -s -X PATCH -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"discount_percent": 10, "discount_label": "Stammkundenrabatt"}' \
  http://localhost:5000/api/v1/quotes/3
```

---

## Quote lines

A quote consists of ordered lines. There are three line types:

| Type | Description |
|---|---|
| `item` | An inventory item (or package) referenced by `item_id`. Prices default to the item's list price; availability and supplier sourcing are handled automatically. |
| `custom` | A free-text position with its own name, quantity and price. |
| `heading` | A free-text section heading. Has no quantity/price and never counts towards totals. |

Additional line behavior:

- **Packages** expand into one line per component, priced proportionally to
  the package price. Package component lines share a `package_id` and move /
  delete as a block.
- **Optional lines** (`is_optional: true`) are shown on the Angebot PDF but
  excluded from the total.
- Adding an `item` line for an item that is already in the quote
  **increments the existing line's quantity** instead of creating a
  duplicate line.

### Add a line

```
POST /api/v1/quotes/<id>/lines
```

| Field | Type | Applies to | Description |
|---|---|---|---|
| `type` | string | all | `item` (default), `custom` or `heading` |
| `item_id` | int | `item` | **Required.** Inventory item or package id (see [Items](#items)) |
| `quantity` | int | `item`, `custom` | Default `1` |
| `price_per_day` | float | `item`, `custom` | For `item`: overrides the default list price |
| `name` | string | `custom`, `heading` | **Required** for these types |

```bash
# Inventory item
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"type": "item", "item_id": 7, "quantity": 2}' \
  http://localhost:5000/api/v1/quotes/3/lines

# Custom position
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"type": "custom", "name": "Aufbau & Technik-Betreuung", "quantity": 1, "price_per_day": 250}' \
  http://localhost:5000/api/v1/quotes/3/lines

# Section heading
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"type": "heading", "name": "Beschallung"}' \
  http://localhost:5000/api/v1/quotes/3/lines
```

Returns `201` with the updated quote. Check `warnings` for availability
conflicts.

### Update a line

```
PATCH /api/v1/quotes/<id>/lines/<line_id>
```

Only fields present in the payload are changed.

| Field | Type | Description |
|---|---|---|
| `quantity` | int | Minimum `1`. Not allowed on headings. |
| `price_per_day` | float | Customer price per unit per day |
| `cost_per_day` | float | External cost per unit per day. Only for custom lines or items **without** supplier entries (otherwise derived from sourcing → `400`). |
| `name` | string | Only for `custom` / `heading` lines |
| `discount_exempt` | bool | Exclude this line from the quote discount |
| `is_optional` | bool | Show on Angebot but exclude from totals |
| `auto_sources` | bool | Re-apply automatic supplier sourcing (own stock first, then cheapest suppliers). Useful after changing `quantity` on an item with suppliers. |

```bash
curl -s -X PATCH -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"quantity": 4, "price_per_day": 12.5, "auto_sources": true}' \
  http://localhost:5000/api/v1/quotes/3/lines/17
```

### Delete a line

```
DELETE /api/v1/quotes/<id>/lines/<line_id>
```

| Query parameter | Description |
|---|---|
| `whole_package=1` | If the line belongs to a package, delete **all** lines of that package |

```bash
curl -s -X DELETE -H "Authorization: Bearer $TOKEN" \
  "http://localhost:5000/api/v1/quotes/3/lines/17?whole_package=1"
```

### Reorder lines

```
POST /api/v1/quotes/<id>/lines/reorder
```

Body: `{"order": [line_id, line_id, ...]}` — line ids in the desired order.
Package components always move as one block: including **any** component id
positions the whole package there. Ids not present in the list keep their
relative order at the end; unknown ids are ignored.

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"order": [21, 17, 19]}' \
  http://localhost:5000/api/v1/quotes/3/lines/reorder
```

---

## Items

### List / search items

```
GET /api/v1/items
```

Lookup endpoint for finding `item_id`s to add to quotes.

| Parameter | Type | Description |
|---|---|---|
| `q` | string | Search across name, manufacturer, model and category (all tokens must match) |
| `start`, `end` | date | If both are given, `available` reflects that rental period; otherwise it is the current operational quantity |

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://localhost:5000/api/v1/items?q=moving+head&start=2026-09-04&end=2026-09-06"
```

```json
{
  "ok": true,
  "items": [
    {
      "id": 7,
      "name": "Moving Head Spot 150W",
      "category": "Licht",
      "price_per_day": 25.0,
      "is_package": false,
      "is_external": false,
      "available": 6
    }
  ]
}
```

> `available: -1` means unlimited.

---

## Objects

### Quote object

```json
{
  "id": 3,
  "reference_number": "RE202609040003",
  "status": "draft",
  "customer_name": "Meyer GmbH",
  "start_date": "2026-09-04",
  "end_date": "2026-09-06",
  "rental_days": 3,
  "rental_days_override": null,
  "created_at": "2026-08-24",
  "prices_are_net": false,
  "subtotal": 450.0,
  "discount_percent": 10.0,
  "discount_label": "Stammkundenrabatt",
  "discount_amount": 45.0,
  "optional_total": 75.0,
  "total": 405.0,

  "recipient_lines": "Meyer GmbH\nMusterstraße 1\n12345 Musterstadt",
  "notes": "internal",
  "public_notes": "printed on PDFs",
  "lines": [ { "...": "see line object" } ]
}
```

- `subtotal`, `discount_amount`, `total` and `optional_total` are computed
  server-side: `total = subtotal − discount_amount`; optional and heading
  lines are excluded from `subtotal`.
- `rental_days` is the billed number of days (override, or date-based).
- The fields below the separator (`recipient_lines`, `notes`,
  `public_notes`, `lines`) are only included in single-quote responses, not
  in the list endpoint.

### Line object

```json
{
  "id": 17,
  "type": "item",
  "name": "Moving Head Spot 150W",
  "item_id": 7,
  "quantity": 2,
  "price_per_day": 25.0,
  "cost_per_day": 10.0,
  "discount_exempt": false,
  "is_optional": false,
  "position": 3,
  "package_id": null,
  "package_name": null,
  "total": 150.0
}
```

- `type` is `item`, `custom` or `heading`. Heading lines only carry `id`,
  `type`, `name` and `position`.
- `total` = `quantity × price_per_day × rental_days`.
- `package_id` / `package_name` are set on lines that were expanded from a
  package.

---

## Error reference

| Status | Meaning |
|---|---|
| `400` | Invalid input (missing required field, bad date format, invalid number, unknown line type, …) |
| `401` | Missing or invalid API token |
| `404` | Quote, line or item not found |
| `409` | Conflict — quote is not in `draft` status, or the package is already in the quote |
| `503` | API disabled (`API_TOKENS` not configured) |
| `500` | Unexpected server error |

All error bodies have the shape:

```json
{ "ok": false, "error": "Angebot ist 'finalized' und kann nicht mehr bearbeitet werden." }
```

> Error messages are in German, matching the rest of the application.
