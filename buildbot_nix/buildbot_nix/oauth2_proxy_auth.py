import base64
import typing
from typing import Any, ClassVar

from buildbot.util import bytes2unicode, unicode2bytes
from buildbot.www.auth import AuthBase, UserInfoProviderBase
from twisted.logger import Logger
from twisted.web.error import Error
from twisted.web.pages import forbidden
from twisted.web.resource import IResource
from twisted.web.server import Request
from twisted.web.util import Redirect

log = Logger()


class OAuth2ProxyAuth(AuthBase):
    header: ClassVar[bytes] = b"Authorization"
    prefix: ClassVar[bytes] = b"Basic "
    password: bytes

    def __init__(self, password: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        if self.userInfoProvider is None:
            self.userInfoProvider = UserInfoProviderBase()
        self.password = unicode2bytes(password)

    def getLoginResource(self) -> IResource:  # noqa: N802
        return forbidden(message="URL is not supported for authentication")

    def getLogoutResource(self) -> IResource:  # noqa: N802
        return typing.cast(IResource, Redirect(b"/oauth2/sign_out"))

    async def maybeAutoLogin(  # noqa: N802
        self, request: Request
    ) -> None:
        header = request.getHeader(self.header)
        if header is None:
            msg = (
                b"missing http header "
                + self.header
                + b". Check your oauth2-proxy config!"
            )
            raise Error(403, msg)
        if not header.startswith(self.prefix):
            msg = (
                b"invalid http header "
                + self.header
                + b". Check your oauth2-proxy config!"
            )
            raise Error(403, msg)
        header = header.removeprefix(self.prefix)
        (username, password) = base64.b64decode(header).split(b":")
        username = bytes2unicode(username)

        if password != self.password:
            msg = b"invalid password given. Check your oauth2-proxy config!!"
            raise Error(403, msg)

        session = request.getSession()  # type: ignore[no-untyped-call]
        user_info = {"username": username}
        if session.user_info != user_info:
            session.user_info = user_info
            await self.updateUserInfo(request)
