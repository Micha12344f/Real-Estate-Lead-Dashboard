# Lead Intelligence n8n source

These twelve sanitized, importable workflow sources preserve the reviewed Lead Intelligence suite. They contain credential references (IDs/names), workflow code and configuration, with no credentials, pinned/static data or execution records. The [manifest](tve-lead-intelligence-manifest.json) records the source checksums and deployment snapshot IDs/states; its `active` fields describe that snapshot, not import instructions.

## Suite

| Workflow | Implementation / snapshot state |
|---|---|
| Agent | Meesix chat, local Ollama models, community Langfuse Agent node; active |
| Search leads, lead context, attention queue | Read-only dashboard queries; active |
| Pipeline summary | Verified exact stage counts and aggregate metrics; active |
| Search properties, property contacts, viewing availability | Read-only property/CRM queries; active |
| Assign property | Existing dashboard association action with original-user confirmation gate; active, write not exercised in verification |
| Send template email | Fixed plain-text templates, backend-resolved recipient and explicit send gate; active, delivery not exercised in verification |
| Create follow-up task, add CRM note | Inactive fail-closed placeholders; no CRM write implementation |

The main workflow uses `n8n-nodes-agent-langfuse.agentLangfuse` version 1 and `@n8n/n8n-nodes-langchain.lmChatOllama`. The community Langfuse package must already be installed. Models are limited to `qwen3.5:27b`, `qwen3:14b` and `qwen3.6:latest`.

## Import and authentication

Import sources as inactive workflows in a separate instance or use targeted updates to the manifest IDs. Production webhook paths already exist; activating duplicate imports would conflict with them. Rebind credential references for another instance:

1. Dashboard/backend → main Agent webhook: **TVE Agent Webhook Auth**, using the dedicated `TVE_AGENT_N8N_WEBHOOK_SECRET`.
2. Main Agent → standalone tool webhooks: **TVE Webhook Auth**.
3. Standalone tools → backend: **TVE Dashboard API - Agent**, plus the configured `Mozilla/5.0 TVE-Agent` User-Agent.
4. Model, traces and fixed-template mail use the referenced Ollama, Langfuse and SMTP credentials respectively.

Provision secret values through the target platform's credential store. The manifest contains metadata only. Task/note placeholders stay inactive and are not connected as Agent tools.

Request: `{message,sessionId,history:[{role,content}],model}`. Response: `{reply,model,toolCalls:[{name,summary}],traceId?}`. Standalone tools return `{ok,data,summary}`. Pipeline output uses `data.stages`, `data.conversion` and `data.metrics`.

The sources preserve the existing execution-retention configuration: standalone tools save no success/error execution data; the main Agent saves execution data/progress. Langfuse metadata labels do not change n8n execution-retention settings.

## Offline verification

From `Apps/Questionnaire-and-Booking/`:

```powershell
node Tools/check_n8n.cjs
```

This validates source hashes (CRLF normalized to LF), credential-reference metadata, absence of embedded runtime data, Code-node syntax, connection endpoints and inactive-placeholder metadata. It makes no network requests or writes. The release sources were also compared as parsed JSON against all twelve sanitized exports. Formatting is compact; workflow content is unchanged.
