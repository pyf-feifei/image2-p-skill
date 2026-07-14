#!/usr/bin/env python3
"""Generate and edit images through an OpenAI-compatible proxy."""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import ipaddress
import mimetypes
import os
import re
import secrets
import socket
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:
    import tomllib
except ImportError as exc:  # pragma: no cover - Python 3.11+ includes tomllib.
    raise SystemExit("Python 3.11 or newer is required") from exc


DEFAULT_PROMPT = (
    "A simple clean test image: one glossy red cube centered on a plain white "
    "background, soft studio lighting, no text, no watermark."
)
DEFAULT_ENV_FILE = Path.home() / ".config" / "image2-p" / "config.env"
MAX_RESPONSE_BYTES = 128 * 1024 * 1024
MAX_ERROR_BYTES = 64 * 1024
VALID_SOURCES = ("auto", "process", "file", "codex")
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class Image2PError(RuntimeError):
    """Expected user-facing failure."""


@dataclass(frozen=True)
class Credentials:
    base_url: str
    api_key: str
    source: str
    mapping: Mapping[str, str] | None = None
    prefix: str = ""


@dataclass(frozen=True)
class ImageResult:
    content: bytes
    detected_format: str


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Prevent bearer credentials from being forwarded across redirects."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate or edit images through an OpenAI-compatible proxy."
    )
    subparsers = parser.add_subparsers(dest="operation", required=True)

    generate = subparsers.add_parser(
        "generate", help="Generate one or more images from a prompt."
    )
    add_common_arguments(generate)
    generate.add_argument("--prompt", default=DEFAULT_PROMPT)
    generate.set_defaults(
        quality="low",
        default_out_stem="output/imagegen/image2-p",
        response_out="output/imagegen/image2-p-response.json",
    )

    edit = subparsers.add_parser(
        "edit", help="Edit one or more source images with a prompt."
    )
    add_common_arguments(edit)
    edit.add_argument("--prompt", required=True)
    edit.add_argument(
        "--image",
        action="append",
        default=[],
        metavar="PATH",
        help="Local source image. Repeat for multiple images.",
    )
    edit.add_argument(
        "--image-url",
        action="append",
        default=[],
        metavar="URL",
        help="Remote source image URL. Repeat for multiple images.",
    )
    edit.add_argument(
        "--mask",
        action="append",
        default=[],
        metavar="PATH",
        help="Optional mask file. Older chatgpt2api versions may ignore it.",
    )
    edit.set_defaults(
        quality="auto",
        default_out_stem="output/imagegen/image2-p-edit",
        response_out="output/imagegen/image2-p-edit-response.json",
    )
    return parser


def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--base-url")
    parser.add_argument("--api-key")
    parser.add_argument("--endpoint")
    parser.add_argument("--source", choices=VALID_SOURCES)
    parser.add_argument("--env-file")
    parser.add_argument("--model")
    parser.add_argument("--n", type=int, default=1)
    parser.add_argument("--size", default="1024x1024")
    parser.add_argument(
        "--quality", choices=("low", "medium", "high", "auto")
    )
    parser.add_argument(
        "--output-format", choices=("png", "jpeg", "webp"), default="png"
    )
    parser.add_argument("--out")
    parser.add_argument("--response-out")
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--allow-insecure-http", action="store_true", default=None)
    parser.add_argument("--dry-run", action="store_true")


def normalize_argv(argv: Sequence[str]) -> list[str]:
    values = list(argv)
    if not values or values[0] not in ("generate", "edit"):
        values.insert(0, "generate")
    return values


def env_file_path(args: argparse.Namespace, process_env: Mapping[str, str]) -> tuple[Path, bool]:
    explicit = args.env_file or process_env.get("IMAGE2_P_ENV_FILE")
    return (Path(explicit).expanduser(), True) if explicit else (DEFAULT_ENV_FILE, False)


def env_file_is_required_for_resolution(
    args: argparse.Namespace,
    operation: str,
    process_env: Mapping[str, str],
    explicit_env_path: bool,
) -> bool:
    if explicit_env_path:
        return True
    if args.base_url or args.api_key:
        return False
    op = operation.upper()
    source_hint = first_nonempty(
        args.source,
        process_env.get(f"IMAGE2_P_{op}_SOURCE"),
        process_env.get("IMAGE2_P_SOURCE"),
    ).lower()
    if source_hint in ("process", "codex"):
        return False
    if source_hint == "file":
        return True
    for prefix in (f"IMAGE2_P_{op}_", "IMAGE2_P_"):
        if first_nonempty(
            process_env.get(f"{prefix}BASE_URL"),
            process_env.get(f"{prefix}API_KEY"),
        ):
            return False
    return True


def parse_env_file(path: Path, required: bool = False) -> dict[str, str]:
    if not path.is_file():
        if required:
            raise Image2PError(f"Variable file not found: {path}")
        return {}
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        raise Image2PError(f"Could not read variable file: {path}") from exc

    values: dict[str, str] = {}
    for number, original in enumerate(lines, start=1):
        line = original.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise Image2PError(f"Invalid variable file line {number}: expected KEY=VALUE")
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not ENV_KEY_RE.fullmatch(key):
            raise Image2PError(f"Invalid variable name on line {number}: {key!r}")
        values[key] = parse_env_value(raw_value.strip(), number)
    return values


def parse_env_value(raw: str, line_number: int) -> str:
    if not raw:
        return ""
    if raw[0] in ("'", '"'):
        quote = raw[0]
        end = find_quote_end(raw, quote)
        if end < 0:
            raise Image2PError(f"Unterminated quote on variable file line {line_number}")
        trailing = raw[end + 1 :].strip()
        if trailing and not trailing.startswith("#"):
            raise Image2PError(f"Unexpected text on variable file line {line_number}")
        value = raw[1:end]
        if quote == '"':
            value = (
                value.replace(r"\n", "\n")
                .replace(r"\r", "\r")
                .replace(r"\t", "\t")
                .replace(r'\"', '"')
                .replace(r"\\", "\\")
            )
        return value
    match = re.search(r"\s+#", raw)
    return raw[: match.start()].rstrip() if match else raw.strip()


def find_quote_end(raw: str, quote: str) -> int:
    escaped = False
    for index in range(1, len(raw)):
        char = raw[index]
        if quote == '"' and char == "\\" and not escaped:
            escaped = True
            continue
        if char == quote and not escaped:
            return index
        escaped = False
    return -1


def requested_source(
    args: argparse.Namespace,
    operation: str,
    process_env: Mapping[str, str],
    file_env: Mapping[str, str],
) -> str:
    op = operation.upper()
    source = first_nonempty(
        args.source,
        process_env.get(f"IMAGE2_P_{op}_SOURCE"),
        process_env.get("IMAGE2_P_SOURCE"),
        file_env.get(f"IMAGE2_P_{op}_SOURCE"),
        file_env.get("IMAGE2_P_SOURCE"),
        "auto",
    ).lower()
    if source not in VALID_SOURCES:
        raise Image2PError(
            f"Invalid configuration source {source!r}; choose {', '.join(VALID_SOURCES)}"
        )
    return source


def resolve_credentials(
    args: argparse.Namespace,
    operation: str,
    process_env: Mapping[str, str],
    file_env: Mapping[str, str],
) -> Credentials:
    cli_pair = pair_from_values(
        args.base_url, args.api_key, "command arguments", None, ""
    )
    if cli_pair:
        return cli_pair

    source = requested_source(args, operation, process_env, file_env)
    op_prefix = f"IMAGE2_P_{operation.upper()}_"
    if source in ("auto", "process"):
        pair = first_pair_from_mapping(
            process_env,
            (
                (op_prefix, "process operation variables"),
                ("IMAGE2_P_", "process common variables"),
            ),
        )
        if pair:
            return pair
    if source in ("auto", "file"):
        pair = first_pair_from_mapping(
            file_env,
            (
                (op_prefix, "variable file operation values"),
                ("IMAGE2_P_", "variable file common values"),
            ),
        )
        if pair:
            return pair
    if source in ("auto", "process"):
        pair = first_pair_from_mapping(
            process_env,
            (("OPENAI_", "legacy OPENAI process variables"),),
        )
        if pair:
            return pair
    if source in ("auto", "codex"):
        return resolve_codex_credentials(process_env)
    raise Image2PError(f"No complete URL/key pair found in the requested {source} source")


def first_pair_from_mapping(
    mapping: Mapping[str, str], prefixes: Iterable[tuple[str, str]]
) -> Credentials | None:
    for prefix, label in prefixes:
        pair = pair_from_values(
            mapping.get(f"{prefix}BASE_URL"),
            mapping.get(f"{prefix}API_KEY"),
            label,
            mapping,
            prefix,
        )
        if pair:
            return pair
    return None


def pair_from_values(
    base_url: str | None,
    api_key: str | None,
    label: str,
    mapping: Mapping[str, str] | None,
    prefix: str,
) -> Credentials | None:
    base_url = (base_url or "").strip()
    api_key = (api_key or "").strip()
    if not base_url and not api_key:
        return None
    if not base_url or not api_key:
        missing = "BASE_URL" if not base_url else "API_KEY"
        raise Image2PError(f"Incomplete {label}: missing {prefix}{missing}")
    return Credentials(base_url, api_key, label, mapping, prefix)


def codex_config_path(process_env: Mapping[str, str]) -> Path:
    codex_home = process_env.get("CODEX_HOME")
    return Path(codex_home).expanduser() / "config.toml" if codex_home else Path.home() / ".codex" / "config.toml"


def resolve_codex_credentials(process_env: Mapping[str, str]) -> Credentials:
    path = codex_config_path(process_env)
    if not path.is_file():
        raise Image2PError(f"Codex configuration not found: {path}")
    try:
        with path.open("rb") as handle:
            config = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise Image2PError(f"Could not parse Codex configuration: {path}") from exc

    shell_policy = config.get("shell_environment_policy")
    if isinstance(shell_policy, dict):
        configured = shell_policy.get("set")
        if isinstance(configured, dict):
            pair = pair_from_values(
                string_value(configured.get("OPENAI_BASE_URL")),
                string_value(configured.get("OPENAI_API_KEY")),
                "Codex shell_environment_policy.set",
                None,
                "",
            )
            if pair:
                return pair

    provider_id = config.get("model_provider")
    providers = config.get("model_providers")
    if not isinstance(provider_id, str) or not isinstance(providers, dict):
        raise Image2PError("Codex config has no active model provider")
    provider = providers.get(provider_id)
    if not isinstance(provider, dict):
        raise Image2PError(f"Codex model provider {provider_id!r} is missing")

    base_url = string_value(provider.get("base_url"))
    api_key = string_value(provider.get("experimental_bearer_token"))
    env_key = string_value(provider.get("env_key"))
    if not api_key and env_key:
        api_key = (process_env.get(env_key) or "").strip()
    pair = pair_from_values(
        base_url,
        api_key,
        f"Codex model provider {provider_id!r}",
        None,
        "",
    )
    if not pair:
        raise Image2PError(f"Codex model provider {provider_id!r} has no credentials")
    return pair


def string_value(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def first_nonempty(*values: str | None) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def setting_value(
    args_value: str | None,
    credentials: Credentials,
    operation: str,
    suffix: str,
    process_env: Mapping[str, str],
    file_env: Mapping[str, str],
    default: str = "",
) -> str:
    if args_value and args_value.strip():
        return args_value.strip()
    op_key = f"IMAGE2_P_{operation.upper()}_{suffix}"
    common_key = f"IMAGE2_P_{suffix}"
    mappings: list[Mapping[str, str]] = []
    if credentials.source.startswith("process") or credentials.source.startswith("legacy"):
        mappings.append(process_env)
    elif credentials.source.startswith("variable file"):
        mappings.append(file_env)
    else:
        # CLI and Codex credentials must not inherit an endpoint or model from
        # another source implicitly.
        mappings = []
    for mapping in mappings:
        value = first_nonempty(mapping.get(op_key), mapping.get(common_key))
        if value:
            return value
    return default


def bool_setting(
    args_value: bool | None,
    operation: str,
    process_env: Mapping[str, str],
    file_env: Mapping[str, str],
) -> bool:
    if args_value is True:
        return True
    op_key = f"IMAGE2_P_{operation.upper()}_ALLOW_INSECURE_HTTP"
    for mapping in (process_env, file_env):
        raw = first_nonempty(
            mapping.get(op_key), mapping.get("IMAGE2_P_ALLOW_INSECURE_HTTP")
        )
        if raw:
            normalized = raw.lower()
            if normalized in ("1", "true", "yes", "on"):
                return True
            if normalized in ("0", "false", "no", "off"):
                return False
            raise Image2PError(f"Invalid boolean value for {op_key}")
    return False


def build_endpoint(base_url: str, operation: str, override: str = "") -> str:
    base_parsed = urllib.parse.urlsplit(base_url)
    validate_api_url(base_parsed)
    if override:
        if override.startswith(("http://", "https://")):
            endpoint = override
        elif override.startswith("/"):
            parsed = urllib.parse.urlsplit(base_url)
            endpoint = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, override, "", ""))
        else:
            endpoint = f"{base_url.rstrip('/')}/{override.lstrip('/')}"
    else:
        normalized = base_url.rstrip("/")
        suffix = "generations" if operation == "generate" else "edits"
        if re.search(r"/images/(generations|edits)$", normalized, re.IGNORECASE):
            endpoint = re.sub(
                r"/images/(generations|edits)$",
                f"/images/{suffix}",
                normalized,
                flags=re.IGNORECASE,
            )
        elif normalized.lower().endswith("/v1"):
            endpoint = f"{normalized}/images/{suffix}"
        else:
            endpoint = f"{normalized}/v1/images/{suffix}"
    parsed = urllib.parse.urlsplit(endpoint)
    validate_api_url(parsed)
    return endpoint


def validate_api_url(parsed: urllib.parse.SplitResult) -> None:
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise Image2PError("Proxy URL must be an absolute http:// or https:// URL")
    if parsed.username or parsed.password:
        raise Image2PError("Proxy URL must not contain embedded credentials")
    if parsed.query or parsed.fragment:
        raise Image2PError("Proxy URL must not contain a query string or fragment")


def ensure_transport_allowed(endpoint: str, allow_insecure: bool) -> None:
    parsed = urllib.parse.urlsplit(endpoint)
    if parsed.scheme != "http" or allow_insecure or is_loopback_host(parsed.hostname or ""):
        return
    raise Image2PError(
        "Refusing non-loopback HTTP because it exposes the bearer key and image data. "
        "Use HTTPS or explicitly set IMAGE2_P_ALLOW_INSECURE_HTTP=true."
    )


def is_loopback_host(host: str) -> bool:
    normalized = host.strip("[]").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def validate_args(args: argparse.Namespace) -> None:
    if not 1 <= args.n <= 4:
        raise Image2PError("--n must be between 1 and 4")
    if args.timeout <= 0:
        raise Image2PError("--timeout must be greater than zero")
    if not args.prompt.strip():
        raise Image2PError("--prompt must not be empty")
    if args.operation == "edit" and not args.image and not args.image_url:
        raise Image2PError("edit requires at least one --image or --image-url")


def validate_local_files(paths: Iterable[str], label: str) -> list[Path]:
    resolved: list[Path] = []
    for raw_path in paths:
        path = Path(raw_path).expanduser().resolve()
        if not path.is_file():
            raise Image2PError(f"{label} file not found: {path}")
        if path.stat().st_size == 0:
            raise Image2PError(f"{label} file is empty: {path}")
        resolved.append(path)
    return resolved


def build_generate_payload(args: argparse.Namespace, model: str) -> dict[str, Any]:
    return {
        "model": model,
        "prompt": args.prompt,
        "n": args.n,
        "size": args.size,
        "quality": args.quality,
        "output_format": args.output_format,
        "response_format": "b64_json",
    }


def build_multipart(
    fields: Sequence[tuple[str, str]],
    images: Sequence[Path],
    masks: Sequence[Path],
) -> tuple[bytes, str]:
    boundary = f"----image2p-{secrets.token_hex(16)}"
    body = bytearray()

    def add_line(value: bytes = b"") -> None:
        body.extend(value)
        body.extend(b"\r\n")

    for name, value in fields:
        add_line(f"--{boundary}".encode("ascii"))
        add_line(f'Content-Disposition: form-data; name="{name}"'.encode("ascii"))
        add_line()
        add_line(value.encode("utf-8"))
    for name, paths in (("image", images), ("mask", masks)):
        for path in paths:
            filename = safe_filename(path.name)
            mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            add_line(f"--{boundary}".encode("ascii"))
            add_line(
                f'Content-Disposition: form-data; name="{name}"; filename="{filename}"'.encode(
                    "utf-8"
                )
            )
            add_line(f"Content-Type: {mime_type}".encode("ascii"))
            add_line()
            body.extend(path.read_bytes())
            body.extend(b"\r\n")
    add_line(f"--{boundary}--".encode("ascii"))
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def safe_filename(filename: str) -> str:
    return filename.replace("\r", "_").replace("\n", "_").replace('"', "_")


def request_json(
    endpoint: str,
    api_key: str,
    body: bytes,
    content_type: str,
    timeout: float,
) -> dict[str, Any]:
    request = urllib.request.Request(
        endpoint,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": content_type,
            "Accept": "application/json",
            "User-Agent": "image2-p/1.0",
        },
    )
    opener = urllib.request.build_opener(NoRedirectHandler())
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = read_limited(response, MAX_RESPONSE_BYTES)
    except urllib.error.HTTPError as exc:
        raw = exc.read(MAX_ERROR_BYTES)
        message = error_message(raw)
        message = redact_secret(message, api_key)
        raise Image2PError(f"Image API returned HTTP {exc.code}: {message}") from exc
    except urllib.error.URLError as exc:
        raise Image2PError(f"Could not reach image API: {exc.reason}") from exc
    except TimeoutError as exc:
        raise Image2PError("Image API request timed out") from exc
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Image2PError("Image API returned a non-JSON response") from exc
    if not isinstance(decoded, dict):
        raise Image2PError("Image API returned an unexpected JSON value")
    return decoded


def read_limited(response: Any, limit: int) -> bytes:
    raw = response.read(limit + 1)
    if len(raw) > limit:
        raise Image2PError("Image API response exceeded the size limit")
    return raw


def error_message(raw: bytes) -> str:
    try:
        decoded = json.loads(raw.decode("utf-8"))
        if isinstance(decoded, dict):
            error = decoded.get("error")
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                return error["message"][:1000]
            if isinstance(decoded.get("message"), str):
                return decoded["message"][:1000]
    except (UnicodeDecodeError, json.JSONDecodeError):
        pass
    return "request failed"


def redact_secret(value: str, secret: str) -> str:
    return value.replace(secret, "<redacted>") if secret else value


def extract_images(
    response: Mapping[str, Any],
    timeout: float,
    endpoint: str,
    allow_insecure: bool,
    expected_count: int,
) -> list[ImageResult]:
    items = response.get("data")
    if not isinstance(items, list) or not items:
        items = response.get("output")
    candidates = (
        [item for item in items if isinstance(item, dict)]
        if isinstance(items, list)
        else []
    )

    results: list[ImageResult] = []
    for item in candidates:
        encoded = first_nonempty(
            string_value(item.get("b64_json")),
            string_value(item.get("result")),
        )
        if encoded:
            results.append(decode_image(encoded))
            continue
        url = string_value(item.get("url"))
        if url.startswith("data:"):
            results.append(decode_data_url(url))
        elif url.startswith(("http://", "https://")):
            results.append(download_image(url, timeout, endpoint, allow_insecure))
    if not results:
        raise Image2PError("Image API response contained no image data")
    if len(results) > expected_count:
        raise Image2PError(
            "Image API returned more images than requested; refusing unexpected output"
        )
    return results


def decode_image(encoded: str) -> ImageResult:
    try:
        content = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise Image2PError("Image API returned invalid base64 image data") from exc
    return validate_image_bytes(content)


def decode_data_url(url: str) -> ImageResult:
    if "," not in url or ";base64" not in url[: url.index(",")]:
        raise Image2PError("Image API returned an unsupported image data URL")
    return decode_image(url.split(",", 1)[1])


def download_image(
    url: str, timeout: float, endpoint: str, allow_insecure: bool
) -> ImageResult:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise Image2PError("Image API returned an invalid image URL")
    if parsed.username or parsed.password:
        raise Image2PError("Image API returned a URL with embedded credentials")
    ensure_transport_allowed(url, allow_insecure)
    ensure_download_target_allowed(url, endpoint)
    request = urllib.request.Request(url, headers={"User-Agent": "image2-p/1.0"})
    opener = urllib.request.build_opener(NoRedirectHandler())
    try:
        with opener.open(request, timeout=timeout) as response:
            content = read_limited(response, MAX_RESPONSE_BYTES)
    except urllib.error.HTTPError as exc:
        raise Image2PError(
            f"Image URL returned HTTP {exc.code}; redirects are not followed"
        ) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise Image2PError("Could not download image URL returned by API") from exc
    return validate_image_bytes(content)


def ensure_download_target_allowed(url: str, endpoint: str) -> None:
    target = urllib.parse.urlsplit(url)
    source = urllib.parse.urlsplit(endpoint)
    target_host = (target.hostname or "").strip("[]").lower()
    if url_origin(target) == url_origin(source):
        return
    if is_loopback_host(target_host):
        raise Image2PError(
            "Image API returned a private or local image URL on a different host"
        )
    try:
        address = ipaddress.ip_address(target_host)
    except ValueError:
        addresses = resolve_host_addresses(target_host, target.port or default_port(target.scheme))
    else:
        addresses = {address}
    if not addresses or any(not address.is_global for address in addresses):
        raise Image2PError(
            "Image API returned a private or local image URL on a different host"
        )


def url_origin(parsed: urllib.parse.SplitResult) -> tuple[str, str, int | None]:
    return (
        parsed.scheme.lower(),
        (parsed.hostname or "").strip("[]").lower(),
        parsed.port or default_port(parsed.scheme),
    )


def default_port(scheme: str) -> int | None:
    return 443 if scheme.lower() == "https" else 80 if scheme.lower() == "http" else None


def resolve_host_addresses(host: str, port: int | None) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise Image2PError("Could not safely resolve image URL host") from exc
    addresses: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    for record in records:
        try:
            addresses.add(ipaddress.ip_address(record[4][0]))
        except ValueError:
            continue
    return addresses


def validate_image_bytes(content: bytes) -> ImageResult:
    if not content:
        raise Image2PError("Decoded image is empty")
    detected = detect_image_format(content)
    if not detected:
        raise Image2PError("API output is not a recognized raster image")
    return ImageResult(content, detected)


def detect_image_format(content: bytes) -> str:
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if content.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "webp"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    return ""


def format_extension(image_format: str) -> str:
    return ".jpeg" if image_format == "jpeg" else f".{image_format}"


def path_matches_format(path: Path, image_format: str) -> bool:
    suffix = path.suffix.lower()
    if image_format == "jpeg":
        return suffix in (".jpg", ".jpeg")
    return suffix == format_extension(image_format)


def output_paths(base: Path, count: int) -> list[Path]:
    if count == 1:
        return [base]
    return [
        base.with_name(f"{base.stem}-{index}{base.suffix}")
        for index in range(1, count + 1)
    ]


def ensure_distinct_paths(
    image_paths: Sequence[Path],
    response_path: Path,
    protected_paths: Sequence[Path] = (),
) -> None:
    normalized = [path.expanduser().resolve() for path in image_paths]
    response = response_path.expanduser().resolve()
    if len(set(normalized)) != len(normalized) or response in normalized:
        raise Image2PError("Image and response output paths must be distinct")
    protected = {path.expanduser().resolve() for path in protected_paths}
    if response in protected or any(path in protected for path in normalized):
        raise Image2PError("Output paths must not overwrite an input image or mask")


def atomic_write(path: Path, content: bytes) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="wb", prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False
    )
    temp_path = Path(handle.name)
    try:
        with handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def redact_json_value(value: Any, secret: str, redact_urls: bool = False) -> Any:
    if isinstance(value, str):
        if redact_urls:
            value = redact_url_query(value)
        return redact_secret(value, secret)
    if isinstance(value, list):
        return [redact_json_value(item, secret, redact_urls) for item in value]
    if isinstance(value, dict):
        sanitized: dict[Any, Any] = {}
        for key, item in value.items():
            sanitized[key] = redact_json_value(
                item, secret, "url" in str(key).lower()
            )
        return sanitized
    return value


def redact_url_query(value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return value
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return value
    if not parsed.query and not parsed.fragment:
        return value
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, "<redacted>", "")
    )


def print_dry_run(
    args: argparse.Namespace,
    endpoint: str,
    credentials: Credentials,
    model: str,
    payload: Mapping[str, Any],
    env_path: Path,
    images: Sequence[Path],
    masks: Sequence[Path],
) -> None:
    summary: dict[str, Any] = {
        "operation": args.operation,
        "endpoint": endpoint,
        "credential_source": credentials.source,
        "credentials": "configured",
        "variable_file": str(env_path.resolve()),
        "payload": redact_json_value(dict(payload), credentials.api_key),
        "output": str(Path(args.out).resolve()),
        "response_output": str(Path(args.response_out).resolve()),
    }
    if args.operation == "edit":
        summary["images"] = [
            {"path": str(path), "bytes": path.stat().st_size} for path in images
        ]
        summary["image_urls"] = [redact_url_query(url) for url in args.image_url]
        summary["masks"] = [
            {"path": str(path), "bytes": path.stat().st_size} for path in masks
        ]
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def run(argv: Sequence[str], process_env: Mapping[str, str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(normalize_argv(argv))
    env = dict(os.environ if process_env is None else process_env)
    try:
        validate_args(args)
        out_was_explicit = args.out is not None
        if not out_was_explicit:
            args.out = f"{args.default_out_stem}{format_extension(args.output_format)}"
        env_path, explicit_env_path = env_file_path(args, env)
        file_required = env_file_is_required_for_resolution(
            args, args.operation, env, explicit_env_path
        )
        if file_required:
            file_env = parse_env_file(env_path, required=explicit_env_path)
        else:
            try:
                file_env = parse_env_file(env_path, required=False)
            except Image2PError:
                file_env = {}
        credentials = resolve_credentials(args, args.operation, env, file_env)
        model = setting_value(
            args.model, credentials, args.operation, "MODEL", env, file_env, "gpt-image-2"
        )
        endpoint_override = setting_value(
            args.endpoint, credentials, args.operation, "ENDPOINT", env, file_env
        )
        endpoint = build_endpoint(credentials.base_url, args.operation, endpoint_override)
        allow_insecure = bool_setting(
            args.allow_insecure_http, args.operation, env, file_env
        )
        ensure_transport_allowed(endpoint, allow_insecure)

        images: list[Path] = []
        masks: list[Path] = []
        payload = build_generate_payload(args, model)
        if args.operation == "edit":
            images = validate_local_files(args.image, "Image")
            masks = validate_local_files(args.mask, "Mask")
            payload["image_url"] = list(args.image_url)

        base_out = Path(args.out)
        response_path = Path(args.response_out)
        protected_paths = [*images, *masks]
        ensure_distinct_paths(
            output_paths(base_out, args.n), response_path, protected_paths
        )

        if args.dry_run:
            print_dry_run(
                args, endpoint, credentials, model, payload, env_path, images, masks
            )
            return 0

        if args.operation == "generate":
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            content_type = "application/json"
        else:
            fields = [
                ("model", model),
                ("prompt", args.prompt),
                ("n", str(args.n)),
                ("size", args.size),
                ("quality", args.quality),
                ("output_format", args.output_format),
                ("response_format", "b64_json"),
            ]
            fields.extend(("image_url", url) for url in args.image_url)
            body, content_type = build_multipart(fields, images, masks)

        response = request_json(
            endpoint, credentials.api_key, body, content_type, args.timeout
        )
        results = extract_images(
            response, args.timeout, endpoint, allow_insecure, args.n
        )
        if not out_was_explicit:
            formats = {result.detected_format for result in results}
            if len(formats) == 1:
                base_out = base_out.with_suffix(format_extension(formats.pop()))
        image_paths = output_paths(base_out, len(results))
        ensure_distinct_paths(image_paths, response_path, protected_paths)

        response_bytes = json.dumps(
            redact_json_value(response, credentials.api_key),
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")
        for path, result in zip(image_paths, results):
            atomic_write(path, result.content)
        atomic_write(response_path, response_bytes)

        for path, result in zip(image_paths, results):
            print(f"wrote image: {path.resolve()} ({result.detected_format})")
            if out_was_explicit and not path_matches_format(path, result.detected_format):
                print(
                    f"warning: {path.name} contains {result.detected_format} data",
                    file=sys.stderr,
                )
        print(f"wrote response: {response_path.resolve()}")
        print(f"operation: {args.operation}")
        print(f"model: {model}")
        print(f"size: {args.size}")
        print(f"quality: {args.quality}")
        return 0
    except (Image2PError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def main() -> int:
    return run(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
