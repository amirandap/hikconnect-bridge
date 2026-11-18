# hikconnect-bridge

Puente local de video en vivo para NVR y cámaras de la **nube de Hik-Connect** (cuentas de consumidor).
Abre **una** sesión con la nube por cámara y la reparte a varios clientes como **MPEG-TS por HTTP**
(HEVC o H.264), más una foto JPEG del último fotograma clave y un endpoint de estado.

> **Estado:** probado de punta a punta con video HEVC real empaquetado en RTP por FFmpeg (55 pruebas).
> **No** se ha probado todavía contra la nube real con esta versión del código. No lo pongas en producción
> antes de correr la prueba real (ver "Pendiente").
>
> **Aviso:** usa una API **no oficial** de Hik-Connect, que puede cambiar sin aviso y cuyas condiciones de
> uso debes revisar. Úsalo solo con cuentas y dispositivos que tengas autorización para acceder.

## Qué hace

```
nube Hik-Connect --(VTM/RTP)--> puente --(MPEG-TS/HTTP)--> ffplay · VLC · go2rtc · ffmpeg
```

- Convierte HEVC/H.264 sobre RTP a Annex B y mantiene la **caché del último GOP**.
- Un **FFmpeg por cliente** (`-c copy`, coste mínimo): cada espectador empieza en un fotograma clave con sus parámetros.
- La sesión con la nube solo existe mientras haya clientes (o una petición de `/frame.jpg`) y se cierra
  `--idle-seconds` después. Si falla, reintenta con espera creciente y *jitter*; si el error es permanente
  (credenciales, cámara inexistente) se pausa 60 s y lo informa en `/health`.
- Mide la cadencia real (`fps_wall`, `fps_rtp_clock`, relación RTP/reloj, mayor hueco entre paquetes).

## Endpoints (solo `127.0.0.1` por defecto)

| Ruta | Qué devuelve |
|---|---|
| `/stream.ts` | Video en vivo, MPEG-TS |
| `/frame.jpg` | JPEG del último IRAP reciente (504 si no llega ninguno) |
| `/health` | Estado, contadores, caché de GOP y métricas de cadencia (errores sin URLs ni claves) |

Seguridad: rechaza `Host` ajeno (DNS rebinding) y peticiones `Sec-Fetch-Site: cross-site`; `--token`
opcional (Bearer o `?token=`) y **obligatorio** fuera de loopback; máximo de clientes (`--max-clients`);
la clave se lee solo del entorno y nunca se imprime.

## Uso

```bash
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
export HIK_CONNECT_USER='...' HIK_CONNECT_PASSWORD='...'      # solo del entorno, nunca en el repo
.venv/bin/hikconnect-bridge serve 90 --stream 2               # cámara #90 del listado, substream
ffplay http://127.0.0.1:8558/stream.ts
curl -s http://127.0.0.1:8558/health
.venv/bin/hikconnect-bridge capture 90 --stream 1 --seconds 15   # guarda Annex B y un JPEG
```

`--stream 1` = principal, `--stream 2` = substream. El número de cámara es el del listado de la cuenta.

## Docker

El `Dockerfile` instala el SDK desde el commit fijado en `HIKCLOUDSTREAM_REF` y trae FFmpeg; no hace falta
Python 3.12 en el equipo. Un proceso atiende **una** cámara (el soporte multi-cámara está pendiente).

```bash
docker build -t hikconnect-bridge https://github.com/amirandap/hikconnect-bridge.git
# bridge.env (permisos 600): HIK_CONNECT_USER=..., HIK_CONNECT_PASSWORD=..., BRIDGE_TOKEN=<token largo aleatorio>
docker run -d --name hikconnect-bridge --restart unless-stopped --env-file bridge.env \
  -p 127.0.0.1:8558:8558 hikconnect-bridge serve 90 --stream 2 --host 0.0.0.0
curl -s -H "Authorization: Bearer $BRIDGE_TOKEN" http://127.0.0.1:8558/health
```

Dentro del contenedor el servicio escucha fuera de loopback, por eso el token es obligatorio; publica el
puerto solo en `127.0.0.1`. Para que otro contenedor lo consuma usa `--allowed-host nombre-del-contenedor`.
Las pruebas se ejecutan con `docker build --target test -t hikconnect-bridge:test .` y `docker run --rm hikconnect-bridge:test -q`.

## Dependencia pendiente de fusionar

La lectura de **todas** las páginas del pagelist VTM y el soporte HEVC/RTP están en un PR a
`hikcloudstream` (ver `PR-DRAFT.md` en su rama `fix/vtm-pagination-hevc-rtp`). Hasta que se fusione, instálalo
desde esa rama; con el `hikcloudstream` publicado solo se ven los primeros 30 recursos de la cuenta y el HEVC no
se decodifica. `pyezvizapi` debe ser `>=1.0.4.9,<2` (las 2.x ya no traen `pyezvizapi.stream`).

## Pruebas

```bash
.venv/bin/python -m pytest          # ~100 s; necesita ffmpeg con libx265 (si falta, se saltan con motivo)
.venv/bin/ruff check src tests
```

Cubren: caché de GOP, colas por cliente, métricas, FFmpeg real, contrato con el SDK, decodificación
de punta a punta, segundo cliente con imagen rápida, cierre por inactividad sin procesos huérfanos,
cliente lento, FFmpeg que muere, errores transitorios y permanentes, `/frame.jpg`, y la seguridad HTTP.

## Pendiente antes de producción

1. Prueba real contra la nube (con una cuenta propia): catálogo, ambos streams, `/health`, `/frame.jpg`.
2. Integración con tu VMS o con go2rtc (por ejemplo como fuente `ffmpeg:` apuntando a `/stream.ts`).
3. Cifrado de video: **no se descifra**; con un NVR cifrado el servicio lo informa y se pausa.
4. Límite de sesiones simultáneas de la cuenta: desconocido.
5. El PR a `hikcloudstream` con la paginación y el HEVC aún no está fusionado.

## Hallazgos que importan al operar

- `-fflags nobuffer` en FFmpeg **pierde fotogramas** cuando llegan en ráfaga (medido: 244 de 298 con un substream real); no usarlo.
- El relay devuelve 30 recursos por página aunque se pida más; el desplazamiento avanza por lo devuelto.
- El stream principal intercala paquetes RTP privados (PT 112) con extensión de encabezado; se ignoran.

## Proyectos relacionados (y en qué se parece este)

Este puente se solapa con proyectos que ya existen; conócelos antes de elegir:

- [hikcloudstream](https://github.com/cristianojmiranda/hikcloudstream): el SDK en el que se apoya este puente
  (login, listado, sesión VTM, depaquetizado). También trae su propio modo `--proxy` (HLS/MJPEG).
- [hikcloud2rtc](https://github.com/halamov/hikcloud2rtc) y su port en Go: servicio HTTP con MPEG-TS HEVC,
  descifrado de video cifrado y vista previa en cuadrícula. No declara licencia; **no se copió código suyo**.
- [cloud-cam-viewer](https://github.com/Bahrombekk/cloud-cam-viewer) (MIT): visor de escritorio y motor de descifrado.
- [pyEzvizApi](https://github.com/RenierM26/pyEzvizApi): protocolo de la nube (login y relay VTM).
- [go2rtc, issue #2289](https://github.com/AlexxIT/go2rtc/issues/2289): una fuente nativa P2P de Hik-Connect
  en discusión; si llega a go2rtc, probablemente sea la integración más directa.

Lo que aporta este repo frente a ellos: un FFmpeg por cliente con caché de GOP (imagen inmediata y sin empezar a
mitad de GOP), ciclo de vida sin procesos huérfanos, protección frente a navegadores y suite de pruebas de punta a punta.
No descifra video cifrado.
