# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability, please report it privately:

1. **Do NOT open a public issue**
2. Email the maintainer or use [GitHub Security Advisories](https://github.com/velesnitski/yt-mcp/security/advisories/new)
3. Include steps to reproduce and potential impact

We will respond within 48 hours and provide a fix timeline.

## Supported Versions

| Version | Supported |
|---|---|
| 1.x | Yes |
| < 1.0 | No |

## Security measures

- YouTrack tokens are passed via environment variables, never hardcoded
- HTTPS enforced by default (HTTP blocked unless `YOUTRACK_ALLOW_HTTP=1`)
- OAuth access code uses `secrets.compare_digest()` (timing-safe)
- OAuth sessions expire after 5 minutes
- CSRF protection on OAuth form
- Error messages truncated to 200 chars to prevent information leakage
- All logs are local — no data sent externally unless `SENTRY_DSN` is set
- Logs never contain tokens, passwords, or issue content
