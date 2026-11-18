"""Fuentes de paquetes RTP: contrato común y la fuente real contra la nube de Hik-Connect."""
from __future__ import annotations

import contextlib
import os
from collections.abc import Callable, Iterator

from hikcloudstream.client import HikConnectClient
from hikcloudstream.exceptions import AuthenticationError
from hikcloudstream.models import ClientConfig, Credentials
from hikcloudstream.stream.session import open_live_stream
from pyezvizapi.stream import VtmChannel

PacketSource = Callable[[], contextlib.AbstractContextManager[Iterator[bytes]]]

API_BASE = "https://api.hik-connect.com"
VTM_OK_CODES = (0, None)


class SourceError(Exception):
    """Fallo transitorio de la fuente: se reintenta con espera creciente."""


class PermanentSourceError(SourceError):
    """Credenciales inválidas o cámara inexistente: reintentar no sirve; se pausa y se avisa."""


class CloudSource:
    """Sesión VTM contra la nube. Reutiliza el login entre aperturas y lo rehace solo si falla."""

    def __init__(self, camera_no: int, stream_type: int, *, user: str | None = None,
                 password: str | None = None) -> None:
        self._camera_no = camera_no
        self._stream_type = stream_type
        self._user = user or os.environ.get("HIK_CONNECT_USER", "")
        self._password = password or os.environ.get("HIK_CONNECT_PASSWORD", "")
        if not self._user or not self._password:
            raise PermanentSourceError("faltan HIK_CONNECT_USER / HIK_CONNECT_PASSWORD en el entorno")
        self._client: HikConnectClient | None = None

    def __repr__(self) -> str:
        return f"CloudSource(camera={self._camera_no}, stream={self._stream_type})"  # nunca incluye credenciales

    def __call__(self) -> contextlib.AbstractContextManager[Iterator[bytes]]:
        return self._open()

    def _login(self) -> HikConnectClient:
        if self._client is not None:
            return self._client
        client = HikConnectClient(ClientConfig(api_base_url=API_BASE))
        try:
            client.login(Credentials(self._user, self._password))
        except AuthenticationError as exc:
            client.close()
            raise PermanentSourceError(f"login rechazado: {type(exc).__name__}") from exc
        self._client = client
        return client

    def _drop_client(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            with contextlib.suppress(Exception):
                client.close()

    @contextlib.contextmanager
    def _open(self) -> Iterator[Iterator[bytes]]:

        client = self._login()
        try:
            camera = next((c for c in client.list_cameras() if c.index == self._camera_no), None)
            if camera is None:
                raise PermanentSourceError(f"la cámara #{self._camera_no} no existe en la cuenta")
            with open_live_stream(client, camera, stream_type=self._stream_type) as session:
                info = session.start()
                if info.result not in VTM_OK_CODES:
                    raise SourceError(f"negociación VTM fallida (código {info.result})")
                yield _video_packets(session, (VtmChannel.STREAM, VtmChannel.ENCRYPTED_STREAM))
        except PermanentSourceError:
            raise
        except Exception:
            self._drop_client()  # sesión posiblemente caducada: la próxima apertura rehace el login
            raise

    def close(self) -> None:
        self._drop_client()


def _video_packets(session, video_channels) -> Iterator[bytes]:  # noqa: ANN001
    # `iter_rtp_packets` es la API pública del SDK; filtra los canales de video y descarta vacíos.
    for packet in session._client.iter_packets():  # noqa: SLF001 - el SDK no expone el flag `encrypted`
        if packet.channel in video_channels and packet.body:
            if packet.encrypted:
                raise PermanentSourceError("el video viene cifrado: el puente no descifra todavía")
            yield packet.body
