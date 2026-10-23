# Retired Linear UI — static design reference

[Frontend guide](../README.md) → [Active dashboard](../dashboard/README.md) → **Archived snapshot**

Retired on 2026-10-05 and superseded by the functional [Lead Intelligence dashboard](../dashboard/README.md), which is served on port 3002. This folder is not served by `frontend/serve-local.js`.

It holds a **fully static snapshot** of the `HEDGEDGE LTD` Linear
workspace, captured from the authenticated app and **completely disconnected from any
back-end**: no Linear client JS, no GraphQL, no network calls.

## Pages

The sidebar is fully cross-linked, so you can click between pages.

| Local file | Linear route | Rendered title |
|---|---|---|
| `index.html` | `/hedgedge-ltd/projects/all` | Projects |
| `home.html` | `/hedgedge-ltd/team/HED/overview` | HEDGEDGE LTD › Overview |
| `issues.html` | `/hedgedge-ltd/team/HED/all` | HEDGEDGE LTD › All issues |
| `views.html` | `/hedgedge-ltd/team/HED/views/issues` | HEDGEDGE LTD › Issues |
| `inbox.html` | `/hedgedge-ltd/inbox` | Inbox |
| `my-issues.html` | `/hedgedge-ltd/my-issues/subscribed` | My issues › Subscribed |
| `reviews.html` | `/hedgedge-ltd/reviews` | Reviews |
| `agent.html` | `/hedgedge-ltd/agent` | New chat |
| `import-export.html` | `/hedgedge-ltd/settings/import-export` | Import & export |

Every page contains the full Linear chrome: workspace switcher, Inbox / My issues /
Reviews / Agent, Workspace (Projects / Views / More), Favorites, Your teams, Import issues,
Invite people, the page header and toolbars, and the **What's new** card.

## How it was produced

1. Opened each route in the authenticated Linear app at a **desktop viewport (1440x900)**,
   finishing any in-flight CSS transitions so each snapshot is stable.
   This matters: Linear encodes *responsive variants into its CSS class names*, so a capture
   taken at a narrow width freezes the mobile/collapsed layout (sidebar closed) into the markup.
2. Read the fully rendered DOM (`document.documentElement.outerHTML`) — the real,
   post-render markup, not the pre-hydration shell.
3. Removed every `<script>` tag (10 per page) and the inert `rel="modulepreload"` hints, so
   the SPA can no longer bootstrap and redirect to `/login`.
4. Rewrote every internal `href` to the matching local file (and unmapped internal routes to
   `#`), so sidebar navigation works offline.
5. Added a `#static-ui-guard` style that hides the `#loading` splash overlay.

Verified: 0 console errors, 0 failed requests, `scriptCount === 0` on every page.

## Viewing the reference

Open `index.html` locally to inspect the archived design. For the functional app and current screenshots, follow the [active dashboard guide](../dashboard/README.md#screenshot-walkthrough).

## Limitations

- **Read-only / no interactivity.** All behaviour came from the removed client JS, so
  buttons, menus, filters, and search do not function. This is a visual snapshot.
- **Responsive breakpoints do not switch.** Because the captures froze the desktop class
  names, the sidebar stays a visible column at every width instead of collapsing into a
  drawer below `1024px` the way the real app does.
- **Only the nine routes above exist.** Links to any other route are inert (`#`).
- **Point-in-time data.** Project/issue lists are frozen as they were at capture time.
- **Styling depends on the public Linear CDN** (`static.linear.app`) for fonts and icons;
  the CSS itself is inlined.
- **Internal only.** The markup reflects a real Linear workspace, so do not publish it to a
  public host or commit it to a public repository.

## Related

- [`../dashboard-original/`](../dashboard-original/) — the previous dashboard prototype.
- [`../README.md`](../README.md) — web host and API proxy guide.
