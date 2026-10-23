# Questionnaire page

[Application guide](../../README.md) → [Frontend](../README.md) → **Questionnaire**

Port 3001 hosts the shared enquiry questionnaire. In both modes, it opens with an expiring contact-bound token; validated answers are stored as flat columns in `questionnaire_responses`. A successful submission advances the contact to `questionnaire_completed` and issues a separate purpose-scoped booking token. Repeated/concurrent submissions preserve completed answers and do not send duplicate booking links.

The success message asks the applicant to check email and spam/junk; it has no direct booking button. A booking link is sent through the mode-selected mail adapter and is also available on the contact dashboard. Viewing requests receive an acknowledgement email.

With `sandbox=TRUE`, invitations and journey mail use the mock inbox even if real SMTP credentials are configured. With `sandbox=FALSE`, invitations, booking links and viewing-request acknowledgements use configured SMTP. Verified legacy signed guest and tokenless submissions still use legacy persistence; see [current implementation limits](../../README.md#current-implementation-limits).

## Vercel production routing

Set `QUESTIONNAIRE_SUBMIT_BASE_URL` to the backend's **complete endpoint**, `https://<backend-host>/webhook/thames-valley-estates/questionnaire-submit`, and `X_TVE_API_KEY` to its webhook credential. The page posts `{payload: {token, answers, consent, ...}}` to `/questionnaire/api/questionnaire-submit`; the proxy unwraps `payload` and forwards it unchanged with server-side authentication. `vercel.json` resolves the API rewrite before the tokenized page rewrite.

Backend `QUESTIONNAIRE_APP_BASE_URL` is the public page base including `/questionnaire`, defaulting in production to `https://thamesvalley-estate-agents.vercel.app/questionnaire`. The questionnaire Vercel project's `BOOKING_APP_BASE_URL` is the booking page base including `/booking`, used for guest catalogue requests. Contact-bound success continues to ask the applicant to check email; it does not reuse the questionnaire token as a booking link.

## Credential settings and applicant links

Staff configure provider/security credentials through [Dashboard Settings](../dashboard/README.md#settings--workspace-credentials). The questionnaire page does not offer credential controls or receive existing provider secrets. Its fixed proxy route supplies webhook authentication server-side; signed tokens are verified by the backend using the current effective signing key.

| Setting change | Questionnaire effect |
|---|---|
| SMTP host/port/user/password | Subsequent production invitation, resend and booking-link sends use the effective configuration resolved immediately before delivery |
| From address | Subsequent messages use the new sender, including sandbox Mailpit messages |
| Token signing key | Newly issued links use the new key; signatures made with a different key are rejected |
| Webhook secret | Changes backend/proxy authentication independently of the applicant token; complete rotation requires aligned proxy/backend recreation |

New contact-bound questionnaire links are purpose-scoped signed opaque tokens stored in the tokens database with expiry/use metadata. Signed guest flows also resolve the current key per issuance/verification. Legacy opaque-token compatibility is described in [backend token rotation](../../backend/README.md#token-signing-key-rotation).

Changing the effective token key through Save or Clear requires confirmation. It does not delete completed questionnaire answers or contact records, but affected applicants need fresh valid links. **Resend invitation** is available for contacts at `enquiried`/`Questionnaire_sent` with an email; it refreshes expiry/consumption and upgrades legacy or stale-key rows. Successfully submitted contacts use the appropriate booking-link issuance flow instead of the early-stage resend action.

Mail credentials are resolved before each send, so panel changes normally require no container restart. Other Python processes refresh cached overrides after the default five-second TTL; a message already being sent is not restarted, and saving a credential does not automatically retry an earlier delivery failure. In Sandbox, host/authentication edits do not redirect outgoing mail to the real provider; Sandbox messages always use the sandbox sender (`lettings@sandbox.invalid` unless a sandbox-specific `TVE_ENQUIRY_FROM` is configured) and Mailpit.

The SMTP connection test is HELO-only and sends no message. It does not prove invitation delivery or SMTP login will work. Public questionnaire/booking URLs, questionnaire fields and the sandbox flag remain startup/application configuration, outside the credential panel. See [backend connection tests](../../backend/README.md#connection-tests) and [dynamic runtime behaviour](../../backend/README.md#dynamic-runtime-behaviour).

Next step: [Booking](../booking/README.md). For invitation delivery and persistence, see [Backend](../../backend/README.md); for startup, see [Docker operations](../../backend/docker/README.md).
