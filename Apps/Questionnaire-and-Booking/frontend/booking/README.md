# Booking page

[Application guide](../../README.md) → [Frontend](../README.md) → **Booking**

Port 3000 hosts the shared booking frontend. In both modes, a contact-bound booking link opens the persisted request flow after questionnaire completion. Production resolves the linked property from the real selectable catalogue (statusId 0 and 1) and reads Acquaint appointments. Sandbox can use synthetic fixtures or configured read-only lookup. Saved requests also block the calendar in both modes.

The calendar uses Europe/London time, 24-hour minimum notice, no Sunday viewings and a Monday start time of noon by default. A saved viewing is displayed when the link is reopened. The flow records `viewing_requested`; `booked` and `done` are not set automatically.

Viewing fields live on `contacts`, not in a separate Bookings database. Booking availability requires a linked selectable property; property overrides are rejected. Production acknowledgements have `localMock=false` and explicitly describe a request awaiting staff confirmation. The adapter does not create CRM appointments. Legacy guest paths retain their compatibility acknowledgements; see [current implementation limits](../../README.md#current-implementation-limits).

## Vercel production routing

- Set `BOOKING_BASE_URL` to the deployed backend's **webhook base**, for example `https://<backend-host>/webhook`, and `X_TVE_API_KEY` to its webhook credential. All booking proxies require these settings; there is no hard-coded n8n fallback.
- The page calls `/booking/api/availability` and `/booking/api/confirm`. `vercel.json` rewrites these to `/api/*` before the `/booking/:path*` page rewrite.
- Availability forwards the token and optional property/contact IDs. Confirm forwards the token in its POST JSON, including when the incoming token was supplied only in the query string. Backend identity/property bindings remain authoritative.
- Backend `BOOKING_APP_BASE_URL` is the public **page base including `/booking`**, defaulting in production to `https://thamesvalley-estate-agents-booking.vercel.app/booking`. Newly issued booking tokens, not questionnaire tokens, form booking links.

## Credential settings and the booking flow

Staff manage integration/security credentials through [Dashboard Settings](../dashboard/README.md#settings--workspace-credentials), not through applicant form inputs. The booking frontend calls fixed local proxy routes; the backend resolves encrypted overrides before environment fallbacks. The browser is never sent Acquaint keys, mail passwords or the vault master key.

| Setting change | Booking effect |
|---|---|
| Acquaint base/API-key/site-prefix | Subsequent backend reads use a client/auth cache for the new credential snapshot; appointment lookup and the selectable property catalogue use those reads |
| SMTP connection or sender | Subsequent booking-link/acknowledgement messages use the effective mail configuration; sandbox keeps Mailpit routing while honoring the sender |
| Token signing key | Links signed with another key fail verification and need replacement through the appropriate journey issuance flow |
| Webhook secret | Affects server-side proxy authentication; dual acceptance preserves the old proxy during rotation until proxy/backend recreation completes it |

Incomplete sandbox Acquaint credentials select the synthetic calendar/catalogue path. Complete credentials that fail authentication or a read produce an integration failure rather than silently supplying successful fixtures. Saving credentials does not create CRM appointments or change a stored viewing request.

Token-key Save/Clear requires confirmation. Rotation changes verification, not the stored contact/questionnaire/viewing data; previously saved requests remain in the contacts database. Older unsigned links have the bootstrap-compatibility behaviour explained in [backend token rotation](../../backend/README.md#token-signing-key-rotation). The backend reads current keys on issuance/verification rather than requiring a rebuild for panel edits.

Calendar rules, public booking URLs and the sandbox mode remain startup configuration. The credential panel does not change minimum notice, available weekdays, slot tuning or which production form paths are migrated. See [dynamic credential timing](../../backend/README.md#dynamic-runtime-behaviour) and [webhook rotation](../../backend/README.md#webhook-authentication-rotation) for propagation and recreation details.

Previous step: [Questionnaire](../questionnaire/README.md). For the API implementation, see [Function Logic](../../backend/app/README.md); for startup, see [Docker operations](../../backend/docker/README.md).
