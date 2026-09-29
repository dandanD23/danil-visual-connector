# Danil Visual Connector

Private Pinterest reference-search connector for the owner's design workflow.

## Components

- Static landing page + Privacy Policy
- Pinterest OAuth Authorization Code flow
- Continuous refresh-token handling
- Read-only Pinterest scopes: `boards:read`, `pins:read`
- Streamable HTTP MCP server at `/mcp`
- Tools for listing boards, listing board Pins, finding boards, and searching saved Pins

## Render environment

Required:
- `PINTEREST_APP_ID=1617457`
- `PINTEREST_APP_SECRET` (set only after Pinterest Trial approval; never commit it)
- `PUBLIC_BASE_URL=https://danil-pinterest-api.onrender.com`
- `PINTEREST_REDIRECT_URI=https://danil-pinterest-api.onrender.com/oauth/pinterest/callback`
- `REDIS_URL=redis://red-dau0crgu01pc73apaff0:6379`

The app intentionally does not request secret-board scopes.
