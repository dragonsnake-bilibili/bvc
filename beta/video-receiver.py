# -*- coding: utf-8 -*-  # noqa: D100, INP001, UP009
import signal
from argparse import ArgumentParser
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib.util import find_spec
from io import BytesIO
from json import dumps, loads
from pathlib import Path
from shutil import which
from subprocess import DEVNULL, PIPE, Popen, run
from sys import executable, orig_argv
from sys import exit as exit_program
from tempfile import NamedTemporaryFile
from threading import Event, Thread
from types import FrameType

if find_spec("PIL.Image") is None:
  print("服务器需要 Pillow，一个广为使用的图像处理库，来处理图片，但你的设备上并未安装")  # noqa: RUF001, T201
  response = input("是否进行安装？这将通过 pip install pillow 完成 [y/N]: ")  # noqa: RUF001
  if response.lower() == "y":
    python = executable or which(orig_argv[0])
    if python is None:
      print("无法找到可运行的 Python 解释器，这很诡异，只好放弃")  # noqa: RUF001, T201
      exit_program()
    command_line = [python, "-m", "pip", "install", "pillow"]
    print(f"正在运行 {command_line}")  # noqa: T201
    run(command_line, check=True)  # noqa: S603
  else:
    exit_program()

from PIL import Image


class _VideoEncoder:
  def __init__(self, path: str, width: int, height: int, fps: int) -> None:
    with NamedTemporaryFile(delete=False, delete_on_close=False, suffix=".mkv") as file:
      self._name = file.name
      self._encoder = Popen(  # noqa: S603
        [
          path,
          "-hide_banner",
          "-y",
          "-f",
          "rawvideo",
          "-pix_fmt",
          "rgba",
          "-video_size",
          f"{width}x{height}",
          "-framerate",
          f"{fps}",
          "-i",
          "-",
          "-an",
          "-vcodec",
          "libvpx-vp9",
          "-crf",
          "4",
          "-b:v",
          "0",
          "-pix_fmt",
          "yuva420p",
          file.name,
        ],
        stdin=PIPE,
        stdout=DEVNULL,
        stderr=DEVNULL,
        close_fds=True,
      )

  def finalize(self) -> str | None:
    if self._encoder is not None:
      self._encoder.communicate()
      self._encoder = None
      return self._name
    return None

  def __del__(self) -> None:
    self.finalize()

  def place_image(self, image: Image.Image) -> None:
    if self._encoder is None or self._encoder.stdin is None:
      return
    self._encoder.stdin.write(image.tobytes())


class _Server(HTTPServer):
  def __init__(
    self,
    server_address: tuple[str, int],
    RequestHandlerClass: Callable[..., BaseHTTPRequestHandler],  # noqa: N803
    ffmpeg_path: str,
  ) -> None:
    """Forward the arguments and setup encoder state."""
    super().__init__(server_address, RequestHandlerClass)
    self.encoder: _VideoEncoder | None = None
    self._ffmpeg_path = ffmpeg_path

  def create_encoder(self, width: int, height: int, fps: int) -> None:
    self.encoder = _VideoEncoder(path=self._ffmpeg_path, width=width, height=height, fps=fps)


class _Handler(BaseHTTPRequestHandler):
  def _handle_meta(self, data: dict) -> None:
    if not isinstance(self.server, _Server):
      self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR)
      return
    method = data.get("method")
    if method not in ("begin", "end", "ping"):
      self.send_error(HTTPStatus.BAD_REQUEST)
      return

    if method == "ping":
      self.send_response(HTTPStatus.NO_CONTENT)
      self.send_header("Access-Control-Allow-Origin", "*")
      self.end_headers()
      return

    if self.server.encoder is not None:
      name = self.server.encoder.finalize()
      self.server.encoder = None
      if method == "end":
        self.send_response(HTTPStatus.OK)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(dumps({"name": name}).encode())
    if method == "begin":
      height, width, fps = data["height"], data["width"], data["fps"]
      self.server.create_encoder(width=width, height=height, fps=fps)
      self.send_response(HTTPStatus.NO_CONTENT)
      self.send_header("Access-Control-Allow-Origin", "*")
      self.end_headers()

  def do_POST(self) -> None:
    if not isinstance(self.server, _Server):
      self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR)
      return

    content_type: str | None = self.headers.get("Content-Type")
    if content_type is None:
      self.send_error(HTTPStatus.BAD_REQUEST)
      return

    raw_payload_length = self.headers.get("Content-Length")
    if raw_payload_length is None:
      self.send_error(HTTPStatus.BAD_REQUEST)
      return

    try:
      payload_length = int(raw_payload_length)
    except ValueError:
      self.send_error(HTTPStatus.BAD_REQUEST)
      return

    if payload_length <= 0:
      self.send_error(HTTPStatus.BAD_REQUEST)
      return

    payload = self.rfile.read(payload_length)

    if content_type == "application/json":
      self._handle_meta(loads(payload))
    elif content_type.startswith("image/"):
      if self.server.encoder is None:
        self.send_error(HTTPStatus.BAD_REQUEST)
        return
      buffer = BytesIO(payload)
      image = Image.open(buffer)
      self.server.encoder.place_image(image)
      self.send_response(HTTPStatus.NO_CONTENT)
      self.send_header("Access-Control-Allow-Origin", "*")
      self.end_headers()
    else:
      self.send_error(HTTPStatus.BAD_REQUEST)

  def do_OPTIONS(self) -> None:
    self.send_response(HTTPStatus.NO_CONTENT)
    self.send_header("Access-Control-Allow-Origin", "*")
    self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
    self.send_header("Access-Control-Allow-Headers", "Content-Type")
    self.end_headers()


def _main() -> None:
  parser = ArgumentParser()
  parser.add_argument("--port", default=8020, type=int, help="接受请求的端口号")
  parser.add_argument("--ffmpeg-executable", type=Path, help="ffmpeg 可执行程序的位置")
  arguments = parser.parse_args()
  ffmpeg_binary = which(arguments.ffmpeg_executable or "ffmpeg")
  if ffmpeg_binary is None:
    print(  # noqa: T201
      "程序需要 FFmpeg 进行视频编码，请确认是否已安装并加入 PATH 环境变量。也可以通过命令行参数指定其位置",  # noqa: RUF001
    )
    exit_program()
  server = _Server(
    server_address=("localhost", arguments.port),
    RequestHandlerClass=_Handler,
    ffmpeg_path=ffmpeg_binary,
  )
  server_thread = Thread(target=server.serve_forever)
  waiter = Event()

  def _stopper(signal_number: int, frame: FrameType | None) -> None:
    _ = signal_number, frame
    server.shutdown()
    server_thread.join()
    waiter.set()

  signal.signal(signal.SIGTERM, _stopper)
  signal.signal(signal.SIGINT, _stopper)
  server_thread.start()

  waiter.wait()


if __name__ == "__main__":
  _main()
