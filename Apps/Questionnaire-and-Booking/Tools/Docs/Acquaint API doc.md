# Acquaint CRM API — Integrators API Reference

This document covers authentication against the Acquaint Integrators API at `api.integrators.acquaintcrm.co.uk` via the `POST /v1/auth` endpoint, and the `GET /v1/Properties/list` property listing endpoint. A short contrast section covers the legacy App API at `api.acquaintcrm.co.uk`, which uses a different authentication scheme (site prefix + app password → `x-acquaint-token` header) and is documented separately.

Last reviewed: 4 October 2026

## Authentication

The Acquaint Integrators API authenticates clients by exchanging a **sitePrefix** and **apiKey** for a JWT, which is then presented as a Bearer token on all subsequent calls.

### Endpoint

- **Method:** `POST`
- **URL:** `https://api.integrators.acquaintcrm.co.uk/v1/auth`
- **operationId:** `Auth_AuthenticateAsync`
- **Summary:** Creates a JWT token used for subsequent api calls
- **Consumes:** `application/json`, `text/json`, `application/xml`, `text/xml`, `application/x-www-form-urlencoded` (JSON is the practical default)

No `Authorization` header is sent on the auth call itself; authentication is purely via the body payload.

### Request body — `AuthRequest`

The body is a single JSON object with two required fields:

| Field | Type | Required | Constraints |
|---|---|---|---|
| sitePrefix | string | Yes | maxLength: 4, minLength: 0 |
| apiKey | string | Yes | (no length constraints) |

Example request body:

```json
{
  "sitePrefix": "TVES",
  "apiKey": "<your-api-key>"
}
```

### The `sitePrefix` parameter

The Swagger `AuthRequest.sitePrefix` field has `maxLength: 4` and `minLength: 0`. This is consistent with the legacy API's `SitePrefix` constraint of "String length: inclusive between 4 and 4", indicating the site prefix is a 4-character code identifying the customer's separate relational database. Each Acquaint customer has their own separate relational database, and each customer database has a unique API Key and Site Prefix. The integrators schema permits 0–4 chars (`minLength` 0), but operationally a valid prefix is 4 characters. The field name in the JSON body is lowercase **sitePrefix**.

## Authentication Response

**Response (HTTP 200):** Schema `Jwt`

| Field | Type | Notes |
|---|---|---|
| token | string | The JWT token string |
| expiresIn | integer (int32) | Lifetime in seconds |

Example response:

```json
{
  "token": "eyJhbGciOi...",
  "expiresIn": 3600
}
```

The **token** field is the sole token carrier — there is no `access_token`, `jwt`, or `bearerToken` field in the schema. The **expiresIn** is an `int32` count of seconds; combined with the landing page's "1 hour lifetime" statement, **expiresIn** will be `3600` in practice.

## Using the JWT

All subsequent API calls require the JWT in the `Authorization` header as a Bearer token. The header format is:

```http
Authorization: Bearer <jwt-token-value>
```

The Swagger testing instruction reinforces this: "enter bearer {jwt_token} into the Swagger api_key field". Note the lowercase `bearer` prefix in the Swagger UI field is a UI convention; the standard HTTP header uses `Bearer` with a capital B.

## Token Expiry & Rate Limiting

### Token lifetime

The JWT Token has a 1 hour lifetime. The **expiresIn** `int32` in the response gives the exact seconds (`3600`). A client should cache the token in-memory and refresh proactively before expiry. A recommended implementation pattern is to refresh approximately 60 seconds before expiry (this is an implementation recommendation, not an API guarantee).

### Rate limiting

Requests are limited to 100 requests per minute, per endpoint, per IP address, per API key. The auth endpoint itself counts against this limit, so clients should not re-authenticate on every call.

### No refresh-token grant

Neither the landing page nor the Swagger spec documents a refresh-token grant. The only documented mechanism to obtain a new JWT is to call `POST /v1/auth` again with the same **sitePrefix** + **apiKey** credentials. There is no documented token-revocation endpoint.

### Recommended client behaviour

Implementation guidance inferred from the primary sources (not an API guarantee):

- Cache `{ token, expiresAt = now + expiresIn - safetyMargin }`.
- Re-call `/v1/auth` when `now >= expiresAt`, or on a `401` from a downstream call.
- Do not re-auth per-request (rate limit is 100/min/endpoint).

## Properties List Endpoint

### Endpoint

- **Method:** `GET`
- **URL:** `https://api.integrators.acquaintcrm.co.uk/v1/Properties/list`
- **operationId:** `Properties_GetListOfProperty`
- **Summary:** Get a list of properties by parameters
- **Produces:** `application/json`, `text/json`, `application/xml`, `text/xml`
- **Authentication:** Requires `Authorization: Bearer <jwt>` header

### Query parameters

All parameters are optional and passed as flat query strings prefixed with `requestPropertyList.`

| Parameter | Type | Notes |
|---|---|---|
| requestPropertyList.propertyName | string | |
| requestPropertyList.street | string | |
| requestPropertyList.locality | string | |
| requestPropertyList.town | string | |
| requestPropertyList.region | string | |
| requestPropertyList.postcode | string | |
| requestPropertyList.altReference | string | alternate/external reference |
| requestPropertyList.countryCode | int32 | |
| requestPropertyList.contactId | int32 | owner/landlord/vendor |
| requestPropertyList.companyId | int32 | |
| requestPropertyList.categoryId | int32 | sales/lettings category |
| requestPropertyList.statusId | int32 | property status |
| requestPropertyList.tenureId | int32 | |
| requestPropertyList.propertyAreaId | int32 | |
| requestPropertyList.minPrice | double | |
| requestPropertyList.maxPrice | double | |
| requestPropertyList.bedrooms | int32 | exact match |
| requestPropertyList.siteId | int32 | |
| requestPropertyList.minUpdatedDate | date-time | ISO 8601, GMT |
| requestPropertyList.maxUpdatedDate | date-time | ISO 8601, GMT |
| requestPropertyList.isStudent | boolean | |
| requestPropertyList.currentPage | int32 | pagination, default 1 |
| requestPropertyList.pageSize | int32 | pagination, default 1000, max 1000 |

### Pagination envelope

Applies to all list endpoints. **currentPage** default `1`, **pageSize** default `1000`, max `1000`. The response is wrapped in a pagination + data envelope; if **currentPage** exceeds total pages, the envelope returns with a blank data array. Dates are GMT and ISO 8601.

### Response (HTTP 200) — `ResponsePagination[PropertyRead]`

```json
{
  "pagination": {
    "currentPage": 1,
    "pageSize": 1000,
    "totalPages": 3,
    "totalRecords": 2847
  },
  "data": [ { } ]
}
```

The pagination object has four `int32` fields: **currentPage**, **pageSize**, **totalPages**, **totalRecords**.

### `PropertyRead` field set

| Field | Type | Notes |
|---|---|---|
| id | int32 | property ID |
| contactId | int32 | owner (landlord/vendor) contact |
| companyId | int32 | |
| categoryId | int32 | sales vs lettings |
| tenureId | int32 | |
| propertyTypeId | int32 | |
| statusId | int32 | property status enum |
| propertyAreaId | int32 | |
| propertyLocalAuthorityId | int32 | |
| propertyTaxBandId | int32 | |
| description | string | |
| fullDescription | string | |
| advertDescription | string | |
| directions | string | |
| virtualTourURL | string | |
| virtualTourURL2 | string | |
| price | double | |
| pricePrefix | int32 | |
| priceFrequency | int32 | |
| bedrooms | int32 | |
| bathrooms | int32 | |
| receptionRooms | int32 | |
| address | AddressBase | nested object (see below) |
| altReference | string | external reference |
| siteId | int32 | |
| addedDate | date-time | ISO 8601, GMT |
| marketDate | date-time | |
| updatedDate | date-time | |
| rentalAvailableDate | date-time | |
| addedByUserId | int32 | |
| usagedId | int32 | (sic — field name as spelled in schema) |
| sellingMethodId | int32 | |
| latitude | double | |
| longitude | double | |
| pictureCount | int32 | |
| hasFloorPlanForWeb | boolean | |
| hasFloorPlanForPrint | boolean | |
| managementTypeId | int32 | |
| bulletPoints | string[] | array of strings |

### `AddressBase` nested object

| Field | Type |
|---|---|
| propertyName | string |
| street | string |
| locality | string |
| town | string |
| region | string |
| postcode | string |
| countryCode | int32 |

## Legacy App API Contrast

The Acquaint platform also exposes a legacy App API at `api.acquaintcrm.co.uk` (documented via its `/Help` pages). It uses a different, older auth contract and should not be confused with the Integrators API.

Key differences:

- **Legacy `POST 17.0/app/auth/login`** takes `LoginCredentials` with fields **userName** (required), **sitePrefix** (required, exactly 4 chars), **appPassword** (required), plus optional **userPassword**, **pcid**, **signalRConnectionId**, **autoAuthToken**. It returns `{ "pcid": 1, "token": "..." }` and the token goes in the `x-acquaint-token` header — not a JWT/Bearer scheme.
- **Legacy `POST 17.0/auth/databasetoken`** takes `SiteCredentials` with **sitePrefix** (required, 4 chars) + **appPassword** (required). It returns `{ "token": "..." }` for use in the `x-acquaint-token` header.

Neither legacy endpoint returns a `Jwt`-shaped object with **expiresIn**, and neither uses **apiKey** as the field name (they use **appPassword**). The Integrators API (`/v1/auth`) is the one that implements the "site prefix + API key → JWT → Bearer" flow.

## Quick Reference

### Auth request

```http
POST https://api.integrators.acquaintcrm.co.uk/v1/auth
Content-Type: application/json

{
  "sitePrefix": "<4-char prefix>",
  "apiKey": "<api-key>"
}
```

### Auth response

```json
{
  "token": "<jwt>",
  "expiresIn": 3600
}
```

### Sample properties list call

```http
GET https://api.integrators.acquaintcrm.co.uk/v1/Properties/list?requestPropertyList.statusId=1&requestPropertyList.currentPage=1&requestPropertyList.pageSize=1000
Authorization: Bearer <jwt>
```

```json
{
  "pagination": {
    "currentPage": 1,
    "pageSize": 1000,
    "totalPages": 3,
    "totalRecords": 2847
  },
  "data": [
    {
      "id": 123,
      "address": { },
      "statusId": 1
    }
  ]
}
```