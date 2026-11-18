"""Línea de comandos: serve (servicio en vivo), capture (foto + .hevc) y probe (formato real del stream)."""
from __future__ import annotations

import argparse
import logging
import signal
import sys
from pathlib import Path

from hikconnect_bridge.capture import collect, extract_first_frame, nal_histogram
from hikconnect_bridge.http_api import BridgeServer
from hikconnect_bridge.service import LiveService
from hikconnect_bridge.sources import CloudSource, PermanentSourceError

DEFAULT_PORT = 8558
DEFAULT_IDLE_S = 30.0
DEFAULT_CAPTURE_S = 15.0
DEFAULT_MAX_CLIENTS = 8
EXIT_USAGE, EXIT_RUNTIME = 2, 3
LOOPBACK = ("127.0.0.1", "localhost", "::1")


def _add_camera_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("camera", type=int, help="número de cámara del listado de la cuenta (1-based)")
    parser.add_argument("--stream", type=int, choices=(1, 2), default=2, help="1 principal, 2 substream")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hikconnect-bridge", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="servicio HTTP en vivo")
    _add_camera_args(serve)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve.add_argument("--idle-seconds", type=float, default=DEFAULT_IDLE_S)
    serve.add_argument("--max-clients", type=int, default=DEFAULT_MAX_CLIENTS)
    serve.add_argument("--token", help="token requerido (Bearer o ?token=); obligatorio fuera de loopback")
    serve.add_argument("--ffmpeg", default="ffmpeg")
    capture = sub.add_parser("capture", help="captura N segundos: guarda el Annex B y el primer fotograma")
    _add_camera_args(capture)
    capture.add_argument("--seconds", type=float, default=DEFAULT_CAPTURE_S)
    capture.add_argument("--out-dir", type=Path, default=Path("out"))
    capture.add_argument("--ffmpeg", default="ffmpeg")
    return parser


def _raise_interrupt(_signum: int, _frame: object) -> None:
    raise KeyboardInterrupt


def cmd_serve(args: argparse.Namespace) -> int:
    if args.host not in LOOPBACK and not args.token:
        print("Fuera de loopback el servicio exige --token (no tiene otra autenticación).", file=sys.stderr)
        return EXIT_USAGE
    try:
        source = CloudSource(args.camera, args.stream)
    except PermanentSourceError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    service = LiveService(source, ffmpeg=args.ffmpeg, idle_seconds=args.idle_seconds,
                          max_clients=args.max_clients)
    server = BridgeServer((args.host, args.port), service, token=args.token)
    signal.signal(signal.SIGTERM, _raise_interrupt)
    base = f"http://{args.host}:{server.server_address[1]}"
    print(f"Cámara #{args.camera} stream={args.stream}\n  ffplay {base}/stream.ts\n  {base}/frame.jpg\n"
          f"  {base}/health\nCtrl+C para salir. La nube solo se usa mientras haya clientes.")
    service.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        service.stop()
        server.server_close()
        source.close()
    return 0


def cmd_capture(args: argparse.Namespace) -> int:
    source: CloudSource | None = None
    try:
        source = CloudSource(args.camera, args.stream)
        annexb, fmt, by_pt = collect(source, args.seconds)
    except PermanentSourceError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_RUNTIME
    finally:
        if source is not None:
            source.close()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    tag = f"cam{args.camera}-s{args.stream}"
    raw = args.out_dir / f"{tag}.{fmt}"
    raw.write_bytes(annexb)
    print(f"paquetes por tipo de carga RTP: {dict(by_pt)}; formato={fmt}; Annex B: {len(annexb)} bytes -> {raw}")
    print("NAL recibidos:", nal_histogram(annexb))
    jpg = args.out_dir / f"{tag}.jpg"
    if extract_first_frame(raw, jpg, fmt, args.ffmpeg):
        print(f"OK fotograma: {jpg} ({jpg.stat().st_size} bytes)")
        return 0
    print("No se pudo decodificar ningún fotograma (prueba --seconds mayor).", file=sys.stderr)
    return EXIT_RUNTIME


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    return {"serve": cmd_serve, "capture": cmd_capture}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
