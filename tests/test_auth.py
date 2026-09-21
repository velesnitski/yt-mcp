"""OAuth provider (ADR-056).

This module was at 0% — it loads lazily, only when an OAuth URL is
configured, so nothing imported it and nothing tested it. It is also the
one place in the server where a mistake is a security bug rather than a
wrong report, so the properties pinned here are the security ones:
single use, binding, expiry and rotation.
"""

import time
from unittest.mock import patch

import pytest
from mcp.shared.auth import OAuthClientInformationFull
from mcp.server.auth.provider import AuthorizationParams
from pydantic import AnyUrl

from yt_mcp import auth

REDIRECT = "https://client.example/callback"


def _client(client_id="client-1"):
    return OAuthClientInformationFull(
        client_id=client_id,
        client_secret="secret",
        redirect_uris=[AnyUrl(REDIRECT)],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope="read",
        token_endpoint_auth_method="client_secret_post",
    )


def _params(state="st"):
    return AuthorizationParams(
        state=state,
        scopes=["read"],
        code_challenge="challenge",
        redirect_uri=AnyUrl(REDIRECT),
        redirect_uri_provided_explicitly=True,
        resource=None,
    )


def _code_from(url):
    return url.split("code=")[1].split("&")[0]


class TestAutoApproveMode:
    """No access code configured: authorize issues a code directly."""

    async def test_authorize_returns_a_redirect_carrying_a_code(self):
        p = auth.SimpleOAuthProvider(server_url="https://srv.example")
        url = await p.authorize(_client(), _params())
        assert url.startswith(REDIRECT)
        assert "code=" in url and "state=st" in url

    async def test_each_authorization_issues_a_distinct_code(self):
        p = auth.SimpleOAuthProvider()
        first = _code_from(await p.authorize(_client(), _params()))
        second = _code_from(await p.authorize(_client(), _params()))
        assert first != second
        assert len(first) > 30, "codes must not be guessable"


class TestAccessCodeMode:
    """An access code is configured: authorize parks the request."""

    def _provider(self):
        return auth.SimpleOAuthProvider(access_code="letmein",
                                        server_url="https://srv.example")

    async def _pending(self, p):
        url = await p.authorize(_client(), _params())
        return url.split("session=")[1]

    async def test_authorize_redirects_to_verification_not_the_client(self):
        p = self._provider()
        url = await p.authorize(_client(), _params())
        assert url.startswith("https://srv.example/auth/verify?session=")
        assert "code=" not in url, "a code must not be issued before verification"

    async def test_correct_code_and_csrf_completes(self):
        p = self._provider()
        session = await self._pending(p)
        csrf = p.get_csrf_for_session(session)
        url = p.verify_and_complete(session, "letmein", csrf)
        assert url and url.startswith(REDIRECT) and "code=" in url

    async def test_wrong_access_code_is_refused(self):
        p = self._provider()
        session = await self._pending(p)
        csrf = p.get_csrf_for_session(session)
        assert p.verify_and_complete(session, "wrong", csrf) is None

    async def test_wrong_csrf_is_refused(self):
        p = self._provider()
        session = await self._pending(p)
        assert p.verify_and_complete(session, "letmein", "not-the-token") is None

    async def test_unknown_session_is_refused(self):
        p = self._provider()
        assert p.verify_and_complete("no-such-session", "letmein", "x") is None
        assert p.get_csrf_for_session("no-such-session") is None

    async def test_a_session_cannot_be_replayed(self):
        """Completing consumes the session; a captured request must not
        be redeemable a second time."""
        p = self._provider()
        session = await self._pending(p)
        csrf = p.get_csrf_for_session(session)
        assert p.verify_and_complete(session, "letmein", csrf) is not None
        assert p.verify_and_complete(session, "letmein", csrf) is None

    async def test_expired_session_is_refused_and_discarded(self):
        p = self._provider()
        session = await self._pending(p)
        csrf = p.get_csrf_for_session(session)
        with patch.object(auth.time, "time", return_value=time.time() + auth._SESSION_EXPIRY + 1):
            assert p.get_csrf_for_session(session) is None
            assert p.verify_and_complete(session, "letmein", csrf) is None

    async def test_csrf_tokens_differ_between_sessions(self):
        p = self._provider()
        a, b = await self._pending(p), await self._pending(p)
        assert p.get_csrf_for_session(a) != p.get_csrf_for_session(b)


class TestAuthorizationCodes:
    async def test_code_is_bound_to_the_client_that_requested_it(self):
        p = auth.SimpleOAuthProvider()
        code = _code_from(await p.authorize(_client("client-1"), _params()))
        assert await p.load_authorization_code(_client("client-1"), code) is not None
        assert await p.load_authorization_code(_client("attacker"), code) is None

    async def test_expired_code_is_refused(self):
        p = auth.SimpleOAuthProvider()
        code = _code_from(await p.authorize(_client(), _params()))
        with patch.object(auth.time, "time", return_value=time.time() + auth._CODE_EXPIRY + 1):
            assert await p.load_authorization_code(_client(), code) is None

    async def test_unknown_code_is_refused(self):
        p = auth.SimpleOAuthProvider()
        assert await p.load_authorization_code(_client(), "made-up") is None

    async def test_code_is_single_use(self):
        """Exchange consumes it — a replayed code must not mint a second
        token."""
        p = auth.SimpleOAuthProvider()
        code = _code_from(await p.authorize(_client(), _params()))
        auth_code = await p.load_authorization_code(_client(), code)
        await p.exchange_authorization_code(_client(), auth_code)
        assert await p.load_authorization_code(_client(), code) is None


class TestTokenExchange:
    async def _tokens(self, p, client=None):
        client = client or _client()
        code = _code_from(await p.authorize(client, _params()))
        auth_code = await p.load_authorization_code(client, code)
        return await p.exchange_authorization_code(client, auth_code)

    async def test_exchange_returns_usable_bearer_tokens(self):
        p = auth.SimpleOAuthProvider()
        tok = await self._tokens(p)
        assert tok.token_type.lower() == "bearer"  # the model normalises case
        assert tok.access_token and tok.refresh_token
        assert tok.access_token != tok.refresh_token
        assert await p.load_access_token(tok.access_token) is not None

    async def test_access_token_expires(self):
        p = auth.SimpleOAuthProvider()
        tok = await self._tokens(p)
        with patch.object(auth.time, "time", return_value=time.time() + auth._TOKEN_EXPIRY + 10):
            assert await p.load_access_token(tok.access_token) is None

    async def test_unknown_access_token_is_refused(self):
        p = auth.SimpleOAuthProvider()
        assert await p.load_access_token("not-a-token") is None

    async def test_refresh_is_bound_to_its_client(self):
        p = auth.SimpleOAuthProvider()
        tok = await self._tokens(p, _client("client-1"))
        assert await p.load_refresh_token(_client("client-1"), tok.refresh_token) is not None
        assert await p.load_refresh_token(_client("attacker"), tok.refresh_token) is None

    async def test_refresh_rotates_and_invalidates_the_old_token(self):
        """A refresh token used once must not work again."""
        p = auth.SimpleOAuthProvider()
        tok = await self._tokens(p)
        rt = await p.load_refresh_token(_client(), tok.refresh_token)
        new = await p.exchange_refresh_token(_client(), rt, ["read"])
        assert new.refresh_token != tok.refresh_token
        assert await p.load_refresh_token(_client(), tok.refresh_token) is None

    async def test_refresh_without_scopes_keeps_the_originals(self):
        p = auth.SimpleOAuthProvider()
        tok = await self._tokens(p)
        rt = await p.load_refresh_token(_client(), tok.refresh_token)
        new = await p.exchange_refresh_token(_client(), rt, [])
        assert new.scope == "read", "dropping scopes silently would widen or narrow access"


class TestRevocation:
    async def test_revoking_an_access_token_stops_it_working(self):
        p = auth.SimpleOAuthProvider()
        code = _code_from(await p.authorize(_client(), _params()))
        auth_code = await p.load_authorization_code(_client(), code)
        tok = await p.exchange_authorization_code(_client(), auth_code)
        access = await p.load_access_token(tok.access_token)
        await p.revoke_token(access)
        assert await p.load_access_token(tok.access_token) is None

    async def test_revoking_a_refresh_token_stops_it_working(self):
        p = auth.SimpleOAuthProvider()
        code = _code_from(await p.authorize(_client(), _params()))
        auth_code = await p.load_authorization_code(_client(), code)
        tok = await p.exchange_authorization_code(_client(), auth_code)
        rt = await p.load_refresh_token(_client(), tok.refresh_token)
        await p.revoke_token(rt)
        assert await p.load_refresh_token(_client(), tok.refresh_token) is None


class TestClientRegistry:
    async def test_registered_client_can_be_retrieved(self):
        p = auth.SimpleOAuthProvider()
        c = _client("abc")
        await p.register_client(c)
        assert (await p.get_client("abc")).client_id == "abc"

    async def test_unregistered_client_is_none(self):
        p = auth.SimpleOAuthProvider()
        assert await p.get_client("nobody") is None
