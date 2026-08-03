from __future__ import annotations

import base64
import typing
from typing import Any, ClassVar

from buildbot.util import unicode2bytes
from buildbot.util.twisted import async_to_deferred
from buildbot.www.auth import AuthBase
from twisted.logger import Logger
from twisted.web.error import Error
from twisted.web.pages import forbidden
from twisted.web.util import Redirect

if typing.TYPE_CHECKING:
    from twisted.web.resource import IResource
    from twisted.web.server import Request

log = Logger()


class OAuth2ProxyAuth(AuthBase):
    authorization_header: ClassVar[bytes] = b"Authorization"
    preferred_username_header: ClassVar[bytes] = b"X-Forwarded-Preferred-Username"
    email_header: ClassVar[bytes] = b"X-Forwarded-Email"
    prefix: ClassVar[bytes] = b"Basic "
    password: bytes

    def __init__(self, password: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.password = unicode2bytes(password)

    def getLoginResource(self) -> IResource:  # noqa: N802
        return forbidden(message="URL is not supported for authentication")

    def getLogoutResource(self) -> IResource:  # noqa: N802
        return typing.cast("IResource", Redirect(b"/oauth2/sign_out"))

    @async_to_deferred
    async def updateUserInfo(self, request: Any) -> None:  # noqa: N802
        session = request.getSession()
        session.updateSession(request)

    async def get_header_checked(self, request: Request, header: bytes) -> bytes:
        header_content: bytes | None = request.getHeader(header)
        if header_content is None:
            msg = (
                b"missing http header " + header + b". Check your oauth2-proxy config!"
            )
            log.error(str(msg))
            raise Error(403, msg)
        return header_content

    @async_to_deferred
    async def maybeAutoLogin(  # noqa: N802
        self, request: Request
    ) -> None:
        authorization = await self.get_header_checked(
            request, self.authorization_header
        )
        if not authorization.startswith(self.prefix):
            msg = (
                b"invalid http header "
                + self.authorization_header
                + b". Check your oauth2-proxy config!"
            )
            log.error(str(msg))
            raise Error(403, msg)
        header = authorization.removeprefix(self.prefix)
        (_, password) = base64.b64decode(header).split(b":")

        if password != self.password:
            msg = b"invalid password given. Check your oauth2-proxy config!!"
            log.error(str(msg))
            raise Error(403, msg)

        preferred_username = await self.get_header_checked(
            request, self.preferred_username_header
        )
        email = await self.get_header_checked(request, self.email_header)

        session = request.getSession()  # type: ignore[no-untyped-call]
        user_info = {
            "username": preferred_username.decode("utf-8"),
            "email": email.decode("utf-8"),
        }
        if session.user_info != user_info:
            session.user_info = user_info
            await self.updateUserInfo(request)
