"""Simple OAuth 2.0 provider for claude.ai connector support.

Implements a passthrough OAuth flow: the server holds YouTrack credentials,
and OAuth with claude.ai is just a gate for access control.

Enable by setting YOUTRACK_OAUTH_URL (the public URL of this server).
"""

import secrets
import time

from pydantic import AnyUrl

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


class SimpleOAuthProvider(OAuthAuthorizationServerProvider):
    """In-memory OAuth provider for single-team deployments.

    Auto-approves authorization requests and issues tokens.
    No user interaction required — the OAuth flow completes automatically.
    """

    def __init__(self) -> None:
        self._clients: dict[str, OAuthClientInformationFull] = {}
        self._auth_codes: dict[str, AuthorizationCode] = {}
        self._access_tokens: dict[str, AccessToken] = {}
        self._refresh_tokens: dict[str, RefreshToken] = {}

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self._clients[client_info.client_id] = client_info

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Auto-approve and redirect back with an authorization code."""
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
        # Remove used code
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
        # Revoke old tokens
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
