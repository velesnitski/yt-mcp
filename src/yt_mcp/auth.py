"""OAuth 2.0 provider for claude.ai connector support.

Two modes:
- Auto-approve (no YOUTRACK_ACCESS_CODE): OAuth flow completes instantly
- Access code gate (YOUTRACK_ACCESS_CODE set): user must enter a code before approval
"""

import secrets
import time

from pydantic import AnyUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse

from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from mcp.server.auth.provider import (
    OAuthAuthorizationServerProvider,
    AuthorizationParams,
    AuthorizationCode,
    RefreshToken,
    AccessToken,
    construct_redirect_uri,
)

# Token lifetime: 24 hours
_TOKEN_EXPIRY = 86400
# Auth code lifetime: 5 minutes
_CODE_EXPIRY = 300

_VERIFY_HTML = """<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>YouTrack MCP — Access Code</title>
    <style>
        body {{ font-family: -apple-system, system-ui, sans-serif; display: flex;
               justify-content: center; align-items: center; min-height: 100vh;
               margin: 0; background: #f5f5f5; }}
        .card {{ background: white; padding: 2rem; border-radius: 12px;
                 box-shadow: 0 2px 8px rgba(0,0,0,0.1); max-width: 400px; width: 90%; }}
        h2 {{ margin: 0 0 0.5rem; color: #333; }}
        p {{ color: #666; font-size: 0.9rem; margin: 0 0 1.5rem; }}
        input {{ width: 100%; padding: 0.75rem; border: 1px solid #ddd; border-radius: 8px;
                 font-size: 1rem; box-sizing: border-box; margin-bottom: 1rem; }}
        button {{ width: 100%; padding: 0.75rem; background: #5A45FF; color: white;
                  border: none; border-radius: 8px; font-size: 1rem; cursor: pointer; }}
        button:hover {{ background: #4835CC; }}
        .error {{ color: #e53e3e; font-size: 0.85rem; margin-bottom: 1rem; }}
    </style>
</head>
<body>
    <div class="card">
        <h2>YouTrack MCP</h2>
        <p>Enter the access code to connect.</p>
        {error}
        <form method="POST">
            <input type="hidden" name="session" value="{session}">
            <input type="password" name="code" placeholder="Access code" autofocus required>
            <button type="submit">Connect</button>
        </form>
    </div>
</body>
</html>"""


class SimpleOAuthProvider(OAuthAuthorizationServerProvider):
    """In-memory OAuth provider for single-team deployments."""

    def __init__(self, access_code: str = "", server_url: str = "") -> None:
        self._access_code = access_code
        self._server_url = server_url.rstrip("/")
        self._clients: dict[str, OAuthClientInformationFull] = {}
        self._auth_codes: dict[str, AuthorizationCode] = {}
        self._access_tokens: dict[str, AccessToken] = {}
        self._refresh_tokens: dict[str, RefreshToken] = {}
        # Pending auth requests waiting for access code verification
        self._pending: dict[str, tuple[OAuthClientInformationFull, AuthorizationParams]] = {}

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self._clients[client_info.client_id] = client_info

    def _create_auth_code(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Generate an auth code and return the redirect URL."""
        code = secrets.token_urlsafe(32)
        self._auth_codes[code] = AuthorizationCode(
            code=code,
            scopes=params.scopes or [],
            expires_at=time.time() + _CODE_EXPIRY,
            client_id=client.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
        )
        return construct_redirect_uri(
            str(params.redirect_uri),
            code=code,
            state=params.state,
        )

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        if not self._access_code:
            # Auto-approve mode
            return self._create_auth_code(client, params)

        # Access code mode: store pending request, redirect to verify page
        session = secrets.token_urlsafe(16)
        self._pending[session] = (client, params)
        return f"{self._server_url}/auth/verify?session={session}"

    def verify_and_complete(self, session: str, code: str) -> str | None:
        """Verify access code and return redirect URL, or None if invalid."""
        if code != self._access_code:
            return None
        pending = self._pending.pop(session, None)
        if not pending:
            return None
        client, params = pending
        return self._create_auth_code(client, params)

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        code = self._auth_codes.get(authorization_code)
        if code and code.client_id == client.client_id and code.expires_at > time.time():
            return code
        return None

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        self._auth_codes.pop(authorization_code.code, None)

        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        expires_at = int(time.time()) + _TOKEN_EXPIRY

        self._access_tokens[access] = AccessToken(
            token=access,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            expires_at=expires_at,
            resource=authorization_code.resource,
        )

        self._refresh_tokens[refresh] = RefreshToken(
            token=refresh,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            expires_at=expires_at + _TOKEN_EXPIRY,
        )

        return OAuthToken(
            access_token=access,
            token_type="bearer",
            expires_in=_TOKEN_EXPIRY,
            refresh_token=refresh,
            scope=" ".join(authorization_code.scopes) if authorization_code.scopes else None,
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        token = self._refresh_tokens.get(refresh_token)
        if token and token.client_id == client.client_id:
            if token.expires_at is None or token.expires_at > time.time():
                return token
        return None

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        self._refresh_tokens.pop(refresh_token.token, None)

        access = secrets.token_urlsafe(32)
        new_refresh = secrets.token_urlsafe(32)
        expires_at = int(time.time()) + _TOKEN_EXPIRY
        use_scopes = scopes or refresh_token.scopes

        self._access_tokens[access] = AccessToken(
            token=access,
            client_id=client.client_id,
            scopes=use_scopes,
            expires_at=expires_at,
        )

        self._refresh_tokens[new_refresh] = RefreshToken(
            token=new_refresh,
            client_id=client.client_id,
            scopes=use_scopes,
            expires_at=expires_at + _TOKEN_EXPIRY,
        )

        return OAuthToken(
            access_token=access,
            token_type="bearer",
            expires_in=_TOKEN_EXPIRY,
            refresh_token=new_refresh,
            scope=" ".join(use_scopes) if use_scopes else None,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        access = self._access_tokens.get(token)
        if access and (access.expires_at is None or access.expires_at > time.time()):
            return access
        return None

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        if isinstance(token, AccessToken):
            self._access_tokens.pop(token.token, None)
        elif isinstance(token, RefreshToken):
            self._refresh_tokens.pop(token.token, None)


def create_verify_handler(provider: SimpleOAuthProvider):
    """Create Starlette endpoint for the access code verification page."""

    async def verify_handler(request: Request):
        if request.method == "GET":
            session = request.query_params.get("session", "")
            return HTMLResponse(_VERIFY_HTML.format(session=session, error=""))

        # POST
        form = await request.form()
        session = str(form.get("session", ""))
        code = str(form.get("code", ""))

        redirect_url = provider.verify_and_complete(session, code)
        if redirect_url:
            return RedirectResponse(url=redirect_url, status_code=302)

        # Wrong code — show form again with error
        return HTMLResponse(_VERIFY_HTML.format(
            session=session,
            error='<p class="error">Invalid access code. Try again.</p>',
        ))

    return verify_handler
