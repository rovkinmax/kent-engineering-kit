from __future__ import annotations

import base64
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import stat
import struct
import sys
import tempfile
import textwrap
import time
import unittest
import zlib
from unittest.mock import patch


MODULE_PATH = Path(__file__).parents[1] / "templates/project/android_tools_native.py"
MODULE_NAME = "android_tools_native_test_target"
SPEC = importlib.util.spec_from_file_location(MODULE_NAME, MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
NATIVE = importlib.util.module_from_spec(SPEC)
sys.modules[MODULE_NAME] = NATIVE
SPEC.loader.exec_module(NATIVE)

VERSION = "4.4.1"


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


def _ui_node_xml(
    class_name: str,
    bounds: str = "[0,0][100,50]",
    *,
    text: str = "",
    resource_id: str = "",
    content_desc: str = "",
    clickable: bool = False,
    password: bool = False,
    naf: bool = False,
    extra_attribute: str | None = None,
) -> str:
    attributes = [
        ("index", "0"),
        ("text", text),
        ("resource-id", resource_id),
        ("class", class_name),
        ("package", "com.example"),
        ("content-desc", content_desc),
        ("checkable", "false"),
        ("checked", "false"),
        ("clickable", str(clickable).lower()),
        ("enabled", "true"),
        ("focusable", "false"),
        ("focused", "false"),
        ("scrollable", "false"),
        ("long-clickable", "false"),
        ("password", str(password).lower()),
        ("selected", "false"),
        ("bounds", bounds),
    ]
    serialized = " ".join(f'{key}="{value}"' for key, value in attributes)
    if naf:
        serialized += ' NAF="true"'
    if extra_attribute is not None:
        serialized += f' {extra_attribute}'
    return f"<node {serialized}/>"


def _ui_xml(*nodes: str) -> str:
    return '<hierarchy rotation="0">' + "".join(nodes) + "</hierarchy>"


PNG = (
    b"\x89PNG\r\n\x1a\n"
    + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 3, 8, 2, 0, 0, 0))
    + _png_chunk(b"IDAT", zlib.compress(b"".join(b"\0" + bytes(6) for _ in range(3))))
    + _png_chunk(b"IEND", b"")
)

INVALID_PNG = (
    b"\x89PNG\r\n\x1a\n"
    + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 3, 8, 2, 0, 0, 0))
    + _png_chunk(b"IDAT", b"fixture")
    + _png_chunk(b"IEND", b"")
)

PNG_INVALID_CASES = {
    "missing_idat": PNG[:33] + _png_chunk(b"IEND", b""),
    "bad_deflate": INVALID_PNG,
    "bad_crc": PNG[:-1] + bytes([PNG[-1] ^ 1]),
    "trailing_bytes": PNG + b"extra",
    "bad_filter": (
        PNG[:33] + _png_chunk(b"IDAT", zlib.compress(b"\x05" + bytes(6) + bytes(14)))
        + _png_chunk(b"IEND", b"")
    ),
    "pixel_limit": (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 16384, 16384, 8, 2, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(b""))
        + _png_chunk(b"IEND", b"")
    ),
    "decoded_limit": (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 4000, 4000, 16, 6, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(b""))
        + _png_chunk(b"IEND", b"")
    ),
}


class SelectorTapTests(unittest.TestCase):
    def run_tap(self, xml, arguments=None, *, custody_error=None, lookup_error=None,
                tap_error=None, callback=True, version_error=None):
        self.events = []
        self.commands = []
        def process(argv, **kwargs):
            self.commands.append(argv)
            self.events.append(argv[1])
            if argv[1] == "--version":
                return NATIVE._ProcessOutput(VERSION.encode(), 0, version_error)
            if argv[1] == "ui-dump":
                return NATIVE._ProcessOutput(xml.encode(), 0, lookup_error)
            self.assertEqual(argv[1], "tap")
            self.assertNotIn("--text", argv)
            self.assertNotIn("--resource-id", argv)
            self.assertTrue(argv[-2].isdigit() and argv[-1].isdigit())
            return NATIVE._ProcessOutput(b"", 1 if tap_error == "provider_failed" else 0,
                                         None if tap_error == "provider_failed" else tap_error)
        def before():
            self.events.append("custody")
            if custody_error:
                raise ValueError("lease changed")
        with patch.object(NATIVE, "_run_process", side_effect=process):
            return NATIVE.execute_android_native(
                "mobile-native", "device.tap",
                {"device_serial": "offline-parser-fixture",
                 **({"resource_id": "login_button"} if arguments is None else arguments)},
                executable=Path(sys.executable), expected_version=VERSION,
                before_effect=before if callback else None,
            )

    def target(self, **kwargs):
        return _ui_node_xml("android.widget.Button", resource_id="login_button",
                            clickable=True, **kwargs)

    def test_unique_target_is_one_coordinate_dispatch_after_fresh_custody(self):
        result = self.run_tap(_ui_xml(self.target()))
        self.assertIsNone(result.error)
        self.assertEqual(self.events, ["--version", "ui-dump", "custody", "tap"])
        self.assertEqual(self.commands[-1][-2:], ("50", "25"))
        self.assertEqual(result.data, {"dispatched": True})
        self.assertNotIn("login_button", json.dumps(result.to_dict()))

    def test_missing_ambiguous_ineligible_or_invalid_target_never_taps(self):
        node = self.target()
        cases = [
            _ui_xml(), _ui_xml(node, node),
            _ui_xml(node.replace('resource-id="login_button"', 'resource-id="login_button_extra"')),
            _ui_xml(node.replace('resource-id="login_button"', 'resource-id="LOGIN_BUTTON"')),
            _ui_xml(node, node.replace('enabled="true"', 'enabled="false"')),
            _ui_xml(node.replace('enabled="true"', 'enabled="false"')),
            _ui_xml(node.replace('enabled="true"', '')),
            _ui_xml(node.replace('clickable="true"', 'clickable="false"')),
            _ui_xml(node.replace('enabled="true"', 'enabled="unknown"')),
        ]
        cases += [_ui_xml(self.target(bounds=bounds)) for bounds in (
            "[0,0][0,50]", "[100,0][50,50]", "[0,0][100001,50]", "malformed",
            "[0,0][" + "9" * 5000 + ",50]",
        )]
        for xml in cases:
            with self.subTest(xml=xml[:100]):
                result = self.run_tap(xml)
                self.assertEqual(result.effect, "not_dispatched")
                self.assertIsNotNone(result.error)
                self.assertNotIn("tap", self.events)
                self.assertNotIn("custody", self.events)
                self.assertIsNone(result.data)

    def test_text_modes_are_explicit_and_assertion_semantics_are_unchanged(self):
        xml = _ui_xml(self.target(text="Continue"))
        for params, accepted in (
            ({"text": "Continue"}, True), ({"text": "continue"}, False),
            ({"text": "Cont"}, False),
            ({"text": "continue", "case_sensitive": False}, True),
            ({"text": "Cont", "text_match": "contains"}, True),
            ({"text": "cont", "text_match": "contains"}, False),
            ({"text": "cont", "text_match": "contains", "case_sensitive": False}, True),
        ):
            with self.subTest(params=params):
                result = self.run_tap(xml, params)
                self.assertEqual(result.error is None, accepted)
                self.assertEqual(self.events.count("tap"), int(accepted))
        nodes, _ = NATIVE._validate_native_ui_xml(xml.encode(), 4096)
        self.assertEqual(NATIVE._count_selector(nodes, "text", "cont"), 1)
        result = self.run_tap(_ui_xml(self.target(content_desc="Continue")), {"text": "Continue"})
        self.assertIsNone(result.error)
        result = self.run_tap(_ui_xml(self.target(text="Continue", password=True)), {"text": "Continue"})
        self.assertEqual(result.effect, "not_dispatched")
        self.assertNotIn("tap", self.events)

    def test_invalid_selector_parameters_fail_before_provider(self):
        for params in (
            {"resource_id": "login_button", "text_match": "exact"},
            {"resource_id": "login_button", "case_sensitive": True},
            {"text": "Continue", "text_match": "regex"},
            {"text": "Continue", "case_sensitive": "false"},
            {"text": "Continue", "x": 0, "y": 0},
            {"x": 0, "y": 0, "text_match": "contains"},
            {"text": "Continue", "resource_id": "login_button"},
        ):
            with self.subTest(params=params):
                result = self.run_tap(_ui_xml(self.target()), params)
                self.assertEqual(result.error, "invalid_arguments")
                self.assertEqual(self.events, [])

    def test_lookup_and_custody_failures_do_not_dispatch_and_tap_failure_stays_unknown(self):
        xml = _ui_xml(self.target())
        for error in ("query_timeout", "interrupted", "drain_unknown"):
            result = self.run_tap(xml, version_error=error)
            self.assertEqual((result.error, result.effect), (error, "not_dispatched"))
            self.assertEqual(self.events, ["--version"])
            result = self.run_tap(xml, lookup_error=error)
            self.assertEqual(result.error, error)
            self.assertEqual(result.effect, "not_dispatched")
            self.assertNotIn("tap", self.events)
        result = self.run_tap(xml, custody_error=True)
        self.assertEqual((result.error, result.effect), ("lease_lost", "not_dispatched"))
        self.assertNotIn("tap", self.events)
        result = self.run_tap(xml, callback=False)
        self.assertEqual((result.error, result.effect), ("lease_lost", "not_dispatched"))
        self.assertEqual(self.events, [])
        for error in ("provider_failed", "query_timeout", "drain_unknown"):
            result = self.run_tap(xml, tap_error=error)
            self.assertEqual((result.error, result.effect), (error, "unknown"))
            self.assertEqual(self.events.count("tap"), 1)

    def test_lookup_and_custody_share_effect_deadline(self):
        with patch.object(NATIVE.time, "monotonic", side_effect=[0, 0, 31]):
            result = self.run_tap(_ui_xml(self.target()))
        self.assertEqual((result.error, result.effect), ("query_timeout", "not_dispatched"))
        self.assertNotIn("tap", self.events)

    def test_enabled_is_optional_for_observation_but_required_for_tap(self):
        xml = _ui_xml(self.target().replace('enabled="true"', ''))
        nodes, _ = NATIVE._validate_native_ui_xml(xml.encode(), 4096)
        self.assertNotIn("enabled", nodes[0])
        result = self.run_tap(xml)
        self.assertEqual((result.error, result.effect), ("assertion_failed", "not_dispatched"))

    def test_nonzero_or_malformed_lookup_never_taps(self):
        self.assertEqual(self.run_tap("not XML").effect, "not_dispatched")
        self.assertNotIn("tap", self.events)
        responses = [NATIVE._ProcessOutput(VERSION.encode(), 0), NATIVE._ProcessOutput(b"", 2)]
        with patch.object(NATIVE, "_run_process", side_effect=responses) as process:
            result = NATIVE.execute_android_native(
                "mobile-native", "device.tap",
                {"device_serial": "offline-parser-fixture", "resource_id": "login_button"},
                executable=Path(sys.executable), expected_version=VERSION,
                before_effect=lambda: self.fail("must not reach custody"),
            )
        self.assertEqual(process.call_count, 2)
        self.assertEqual((result.error, result.effect), ("provider_failed", "not_dispatched"))


class AndroidToolsNativeTest(unittest.TestCase):
    def test_shared_png_bytes_validator(self) -> None:
        self.assertEqual(NATIVE.validate_png_bytes(PNG), (2, 3))
        for name, content in PNG_INVALID_CASES.items():
            with self.subTest(name=name), self.assertRaises(NATIVE._InputError):
                NATIVE.validate_png_bytes(content)

    def test_platform_metadata_preserves_projection(self) -> None:
        node = _ui_node_xml("android.view.View", text="Public")
        baseline = NATIVE._validate_native_ui_xml(_ui_xml(node).encode(), 1048576)
        for order in ("-2147483648", "-1", "0", "1", "2147483647"):
            with self.subTest(order=order):
                xml = _ui_xml(node.replace(
                    "<node ", f'<node drawing-order="{order}" hint="PRIVATE_HINT_SENTINEL" ', 1
                ))
                actual = NATIVE._validate_native_ui_xml(xml.encode(), 1048576)
                self.assertEqual(actual, baseline)
                self.assertNotIn("PRIVATE_HINT_SENTINEL", json.dumps(actual))

    def test_discarded_platform_metadata_does_not_require_known_types(self) -> None:
        baseline_node = _ui_node_xml("android.view.View")
        baseline = NATIVE._validate_native_ui_xml(_ui_xml(baseline_node).encode(), 1048576)
        for value in ("", "+1", "-0", "01", "-01", " 1", "1 ", "1.0", "1e0",
                      "١", "2147483648", "-2147483649", "12345678901234567890"):
            with self.subTest(value=value):
                node = baseline_node.replace(
                    "<node ", f'<node drawing-order="{value}" ', 1
                )
                self.assertEqual(NATIVE._validate_native_ui_xml(_ui_xml(node).encode(), 1048576),
                                 baseline)

    def test_hint_is_discarded_for_every_node_sensitivity(self) -> None:
        for class_name, password, resource_id in (
            ("android.view.View", False, ""),
            ("android.view.View", True, ""),
            ("android.widget.EditText", False, ""),
            ("android.view.View", False, "app:id/password"),
        ):
            for hint in ("", "Public example", "password=PRIVATE_HINT_SENTINEL"):
                with self.subTest(class_name=class_name, password=password, hint=hint):
                    node = _ui_node_xml(class_name, password=password, resource_id=resource_id,
                                        text="Public", content_desc="Label")
                    baseline = NATIVE._validate_native_ui_xml(_ui_xml(node).encode(), 1048576)
                    xml = _ui_xml(node.replace("<node ", f'<node hint="{hint}" ', 1))
                    actual = NATIVE._validate_native_ui_xml(xml.encode(), 1048576)
                    self.assertEqual(actual, baseline)
                    self.assertNotIn("hint", actual[0][0])
                    self.assertNotIn("PRIVATE_HINT_SENTINEL", json.dumps(actual))

    def test_unknown_response_metadata_is_not_projected(self) -> None:
        baseline = NATIVE._validate_native_ui_xml(
            _ui_xml(_ui_node_xml("android.view.View")).encode(), 1048576,
        )
        node = _ui_node_xml("android.view.View").replace(
            "<node ", '<node hint="" drawing-order="0" unknown-platform-key="PRIVATE_SENTINEL" ', 1
        )
        xml = _ui_xml(node).replace(
            '<hierarchy rotation="0"', '<hierarchy rotation="PRIVATE_SENTINEL" vendor="PRIVATE_SENTINEL"',
        )
        self.assertEqual(NATIVE._validate_native_ui_xml(xml.encode(), 1048576), baseline)

    def test_projected_fields_remain_required_and_typed(self) -> None:
        node = _ui_node_xml("android.view.View")
        for attribute in ('class="android.view.View"', 'bounds="[0,0][100,50]"',
                          'clickable="false"', 'password="false"'):
            with self.subTest(missing=attribute):
                self.assertIn(attribute, node)
                with self.assertRaises(NATIVE._InputError):
                    NATIVE._validate_native_ui_xml(
                        _ui_xml(node.replace(attribute, "")).encode(), 1048576,
                    )
        for attribute in ("clickable", "password", "NAF", "enabled"):
            with self.subTest(invalid=attribute):
                value = "true" if attribute == "enabled" else "false"
                candidate = node.replace(f'{attribute}="{value}"', f'{attribute}="unknown"')
                if attribute == "NAF":
                    candidate = node.replace("<node ", '<node NAF="unknown" ', 1)
                with self.assertRaises(NATIVE._InputError):
                    NATIVE._validate_native_ui_xml(_ui_xml(candidate).encode(), 1048576)
        for attribute in ("checked", "focused"):
            value = "true" if attribute == "enabled" else "false"
            candidate = node.replace(f'{attribute}="{value}"', f'{attribute}="new-format"')
            self.assertEqual(
                NATIVE._validate_native_ui_xml(_ui_xml(candidate).encode(), 1048576),
                NATIVE._validate_native_ui_xml(_ui_xml(node).encode(), 1048576),
            )

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.executable = self.root / "fake-android-cli"
        self.log_path = self.root / "calls.jsonl"
        self.child_marker = self.root / "child-terminated"
        self.outside = self.root / "outside.png"
        self.executable.write_text(
            textwrap.dedent(
                f"""\
                #!{sys.executable}
                import base64
                import json
                import os
                from pathlib import Path
                import subprocess
                import sys
                import time

                args = sys.argv[1:]
                if args and args[-1] == "--version":
                    print(os.environ.get("FAKE_VERSION", "{VERSION}"))
                    raise SystemExit(0)

                with open(os.environ["FAKE_CALLS"], "a", encoding="utf-8") as stream:
                    stream.write(json.dumps(args) + chr(10))
                mode = os.environ.get("FAKE_MODE", "success")

                if mode == "large":
                    sys.stdout.write("x" * 100000)
                elif mode == "stdout":
                    sys.stdout.write(os.environ.get("FAKE_STDOUT", ""))
                elif mode == "sentinel":
                    marker = os.environ.get("FAKE_STDOUT", "")
                    sys.stdout.write(marker)
                    sys.stderr.write(os.environ.get("FAKE_STDERR", marker))
                elif mode == "error":
                    sys.stderr.write(os.environ.get("FAKE_STDERR", "private-marker-secret\\n"))
                    raise SystemExit(7)
                elif mode in {"png", "png-invalid"}:
                    output = next(
                        (args[index + 1] for index, value in enumerate(args)
                         if value == "--output"),
                        None,
                    )
                    if output is None:
                        output = next(
                            (value.split("=", 1)[1] for value in args
                             if value.startswith("--output=")),
                            None,
                        )
                    if output is not None:
                        variable = "FAKE_INVALID_PNG" if mode == "png-invalid" else "FAKE_PNG"
                        Path(output).write_bytes(base64.b64decode(os.environ[variable]))
                elif mode in {
                    "preview",
                    "preview-traversal",
                    "preview-deep",
                    "preview-too-deep",
                }:
                    output = next(
                        (value.split("=", 1)[1] for value in args
                         if value.startswith("--output=")),
                        None,
                    )
                    artifact_dir = Path(output) / "com" / "example"
                    artifact_dir.mkdir(parents=True)
                    image = artifact_dir / "LoginPreview_0.png"
                    tree = artifact_dir / "LoginPreview_0.json"
                    image.write_bytes(base64.b64decode(os.environ["FAKE_PNG"]))
                    components = []
                    if mode in {"preview-deep", "preview-too-deep"}:
                        component = {{"name": "leaf"}}
                        depth = 33 if mode == "preview-too-deep" else 16
                        for _ in range(depth):
                            component = {{"children": [component]}}
                        components = [component]
                    tree.write_text(json.dumps({{
                        "components": components,
                        "densityDpi": 420,
                        "deviceId": "pixel",
                        "fontScale": 1.0,
                        "height": 1600,
                        "methodFQN": "com.example.LoginPreview",
                        "previewId": "LoginPreview",
                        "uiMode": "normal",
                        "width": 900,
                        "apiToken": "private-tree-marker",
                    }}))
                    print(json.dumps({{
                        "success": True,
                        "results": [{{
                            "previewId": "LoginPreview",
                            "methodFqn": "com.example.LoginPreview",
                            "imagePath": (
                                os.environ["FAKE_OUTSIDE"]
                                if mode == "preview-traversal"
                                else str(image)
                            ),
                            "componentTreePath": str(tree),
                        }}],
                    }}))
                elif mode == "symlink":
                    output = next(
                        (args[index + 1] for index, value in enumerate(args)
                         if value == "--output"),
                        None,
                    )
                    if output is None:
                        output = next(
                            (value.split("=", 1)[1] for value in args
                             if value.startswith("--output=")),
                            None,
                        )
                    Path(output).symlink_to(Path(os.environ["FAKE_OUTSIDE"]))
                elif mode == "spawn":
                    marker = os.environ["FAKE_MARKER"]
                    child = (
                        "import pathlib,signal,time; "
                        f"signal.signal(signal.SIGTERM, lambda *_: pathlib.Path({{marker!r}}).write_text('term')); "
                        "time.sleep(30)"
                    )
                    subprocess.Popen([sys.executable, "-c", child])
                    time.sleep(30)
                raise SystemExit(0)
                """
            ),
            encoding="utf-8",
        )
        self.executable.chmod(0o700)
        self.outside.write_bytes(PNG)
        self.stage = self.root / "stage"
        self.stage.mkdir()
        self.stage.chmod(0o700)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def call(
        self,
        provider: str,
        operation: str,
        arguments: dict[str, object],
        *,
        mode: str = "success",
        stdout: str = "",
        stderr: str = "",
        timeout: float = 2.0,
        output_limit: int = 64 * 1024,
        source_root: Path | None = None,
        staging_dir: Path | None = None,
        version: str = VERSION,
        before_effect=None,
    ):
        environment = {
            "FAKE_CALLS": str(self.log_path),
            "FAKE_MODE": mode,
            "FAKE_STDOUT": stdout,
            "FAKE_STDERR": stderr,
            "FAKE_VERSION": version,
            "FAKE_PNG": base64.b64encode(PNG).decode("ascii"),
            "FAKE_INVALID_PNG": base64.b64encode(INVALID_PNG).decode("ascii"),
            "FAKE_OUTSIDE": str(self.outside),
            "FAKE_MARKER": str(self.child_marker),
        }
        with patch.dict(os.environ, environment):
            return NATIVE.execute_android_native(
                provider,
                operation,
                arguments,
                executable=self.executable,
                expected_version=VERSION,
                timeout_s=timeout,
                output_limit_bytes=output_limit,
                source_root=source_root,
                staging_dir=staging_dir,
                before_effect=before_effect,
            )

    def calls(self) -> list[list[str]]:
        if not self.log_path.exists():
            return []
        return [json.loads(line) for line in self.log_path.read_text().splitlines()]

    def test_docs_search_returns_bounded_useful_redacted_output(self) -> None:
        result = self.call(
            "google-android",
            "docs.search",
            {"query": "Compose previews"},
            mode="stdout",
            stdout='{"matches":[{"title":"Previews","api_token":"abc123"}]}',
        )

        self.assertEqual(result.transport, "completed")
        self.assertIsNone(result.error)
        self.assertEqual(result.data["matches"][0]["title"], "Previews")
        self.assertEqual(result.data["matches"][0]["api_token"], "<redacted>")
        self.assertNotIn("abc123", json.dumps(result.to_dict()))
        self.assertEqual(
            self.calls(),
            [["--no-metrics", "docs", "search", "Compose previews"]],
        )

    def test_secret_text_is_rejected_before_provider_dispatch(self) -> None:
        result = self.call(
            "google-android",
            "docs.search",
            {"query": "token=private-marker"},
        )

        self.assertEqual(result.error, "secret_transport_unsupported")
        self.assertEqual(result.effect, "not_dispatched")
        self.assertEqual(self.calls(), [])

    def test_provider_version_mismatch_blocks_operation_dispatch(self) -> None:
        result = self.call(
            "google-android",
            "docs.search",
            {"query": "compose"},
            version="unexpected",
        )

        self.assertEqual(result.error, "provider_version_mismatch")
        self.assertEqual(result.effect, "not_dispatched")
        self.assertEqual(self.calls(), [])

    def test_missing_docs_output_is_categorical(self) -> None:
        result = self.call(
            "google-android",
            "docs.fetch",
            {"url": "kb://compose/preview"},
        )

        self.assertEqual(result.error, "missing_output")
        self.assertEqual(result.transport, "completed")

    def test_provider_error_does_not_expose_stderr(self) -> None:
        result = self.call(
            "google-android",
            "docs.search",
            {"query": "compose"},
            mode="error",
        )

        self.assertEqual(result.error, "provider_failed")
        self.assertNotIn("private-marker", json.dumps(result.to_dict()))

    def test_output_limit_is_enforced_while_reading(self) -> None:
        result = self.call(
            "google-android",
            "docs.search",
            {"query": "compose"},
            mode="large",
            output_limit=1024,
        )

        self.assertEqual(result.error, "observation_limit")
        self.assertEqual(result.transport, "failed")

    def test_timeout_settles_grandchild_process_group(self) -> None:
        started = time.monotonic()
        result = self.call(
            "mobile-native",
            "device.tap",
            {"device_serial": "emulator-5554", "x": 50, "y": 25},
            mode="spawn",
            timeout=0.2,
        )

        self.assertLess(time.monotonic() - started, 3.5)
        self.assertEqual(result.error, "query_timeout")
        self.assertEqual(result.transport, "timed_out")
        self.assertEqual(result.effect, "unknown")
        self.assertTrue(self.child_marker.exists())

    def test_invalid_or_missing_serial_never_dispatches(self) -> None:
        result = self.call(
            "mobile-native",
            "device.tree",
            {"device_serial": "emulator-5554 --device another"},
        )

        self.assertEqual(result.error, "device_serial_required")
        self.assertEqual(result.effect, "not_dispatched")
        self.assertEqual(self.calls(), [])

    def test_successful_device_dispatch_does_not_claim_observed_effect(self) -> None:
        result = self.call(
            "mobile-native",
            "device.key",
            {"device_serial": "emulator-5554", "key": "back"},
        )

        self.assertEqual(result.transport, "completed")
        self.assertEqual(result.effect, "unknown")
        self.assertEqual(result.data, {"dispatched": True})
        self.assertEqual(
            self.calls(),
            [["key", "--platform", "android", "--device", "emulator-5554", "back"]],
        )

    def test_input_uses_one_closed_positional_value_and_reports_dispatch_only(self) -> None:
        value = "ordinary value; $(not-shell-expanded)"
        custody_checks = []
        result = self.call(
            "mobile-native",
            "device.input",
            {"device_serial": "emulator-5554", "text": value},
            before_effect=lambda: custody_checks.append("checked"),
        )

        self.assertEqual(result.transport, "completed")
        self.assertEqual(result.effect, "unknown")
        self.assertIsNone(result.error)
        self.assertEqual(result.data, {"dispatched": True})
        self.assertEqual(custody_checks, ["checked"])
        self.assertEqual(
            self.calls(),
            [["input", "--platform", "android", "--device", "emulator-5554", value]],
        )
        self.assertNotIn(value, json.dumps(result.to_dict()))

    def test_input_rejects_open_arguments_and_invalid_values_before_provider(self) -> None:
        cases = (
            ({"device_serial": "emulator-5554"}, "invalid_arguments"),
            ({"device_serial": "emulator-5554", "text": ""}, "invalid_arguments"),
            ({"device_serial": "emulator-5554", "text": "x" * 4097}, "invalid_arguments"),
            ({"device_serial": "emulator-5554", "text": "line\nbreak"}, "invalid_arguments"),
            ({"device_serial": "emulator-5554", "text": "secret=value"},
             "secret_transport_unsupported"),
            ({"device_serial": "emulator-5554", "text": "harmless", "package": "com.example.app"},
             "invalid_arguments"),
            ({"device_serial": "emulator-5554", "text": "harmless", "selector": "EditText"},
             "invalid_arguments"),
        )
        for arguments, error in cases:
            with self.subTest(arguments=arguments):
                result = self.call("mobile-native", "device.input", arguments)
                self.assertEqual((result.error, result.effect), (error, "not_dispatched"))
                self.assertEqual(self.calls(), [])

    def test_input_requires_custody_recheck_and_loss_prevents_provider_dispatch(self) -> None:
        missing = self.call(
            "mobile-native", "device.input",
            {"device_serial": "emulator-5554", "text": "ordinary"},
        )
        lost = self.call(
            "mobile-native", "device.input",
            {"device_serial": "emulator-5554", "text": "ordinary"},
            before_effect=lambda: (_ for _ in ()).throw(ValueError("lease lost")),
        )

        self.assertEqual((missing.error, missing.effect), ("lease_lost", "not_dispatched"))
        self.assertEqual((lost.error, lost.effect), ("lease_lost", "not_dispatched"))
        self.assertEqual(self.calls(), [])

    def test_input_suppresses_provider_sentinels_and_never_retries_unknown_effect(self) -> None:
        value = "private-input-sentinel"
        output = "private-output-sentinel"
        sentinels = self.call(
            "mobile-native", "device.input",
            {"device_serial": "emulator-5554", "text": value},
            mode="sentinel", stdout=output, stderr=value, before_effect=lambda: None,
        )
        failed = self.call(
            "mobile-native", "device.input",
            {"device_serial": "emulator-5554", "text": value},
            mode="error", stderr=value, before_effect=lambda: None,
        )
        timed_out = self.call(
            "mobile-native", "device.input",
            {"device_serial": "emulator-5554", "text": value},
            mode="spawn", timeout=0.2, before_effect=lambda: None,
        )

        self.assertEqual((sentinels.transport, sentinels.effect, sentinels.error),
                         ("completed", "unknown", None))
        self.assertEqual((failed.transport, failed.effect, failed.error),
                         ("failed", "unknown", "provider_failed"))
        self.assertEqual((timed_out.transport, timed_out.effect, timed_out.error),
                         ("timed_out", "unknown", "query_timeout"))
        for result in (sentinels, failed, timed_out):
            self.assertNotIn(value, json.dumps(result.to_dict()))
            self.assertNotIn(output, json.dumps(result.to_dict()))
        self.assertEqual(len(self.calls()), 3)
        self.assertTrue(all(call[0] == "input" for call in self.calls()))

    def test_input_unconfirmed_descendant_cleanup_is_unknown_without_replay(self) -> None:
        settle_group = NATIVE._settle_group

        def settle_but_report_unknown(process, deadline):
            settled = settle_group(process, deadline)
            if process.args[-1] == "--version":
                return settled
            return False

        value = "private-input-sentinel"
        with patch.object(NATIVE, "_settle_group", side_effect=settle_but_report_unknown):
            result = self.call(
                "mobile-native",
                "device.input",
                {"device_serial": "emulator-5554", "text": value},
                mode="spawn",
                timeout=0.2,
                before_effect=lambda: None,
            )

        self.assertEqual(
            (result.transport, result.effect, result.error),
            ("unknown", "unknown", "drain_unknown"),
        )
        self.assertNotIn(value, json.dumps(result.to_dict()))
        self.assertEqual(
            self.calls(),
            [["input", "--platform", "android", "--device", "emulator-5554", value]],
        )
        self.assertTrue(self.child_marker.exists())

    def test_dpad_keys_dispatch_exact_aliases_case_insensitively(self) -> None:
        aliases = (
            ("up", "up"),
            ("down", "down"),
            ("left", "left"),
            ("right", "right"),
            ("center", "KEYCODE_DPAD_CENTER"),
        )
        results = [
            self.call(
                "mobile-native",
                "device.key",
                {"device_serial": "emulator-5554", "key": alias.upper()},
            )
            for alias, _ in aliases
        ]

        self.assertEqual(
            self.calls(),
            [
                [
                    "key",
                    "--platform",
                    "android",
                    "--device",
                    "emulator-5554",
                    native_key,
                ]
                for _, native_key in aliases
            ],
        )
        for result in results:
            with self.subTest(result=result):
                self.assertEqual(result.transport, "completed")
                self.assertEqual(result.effect, "unknown")
                self.assertEqual(result.data, {"dispatched": True})

    def test_center_is_distinct_from_enter(self) -> None:
        center = self.call(
            "mobile-native",
            "device.key",
            {"device_serial": "emulator-5554", "key": "center"},
        )
        enter = self.call(
            "mobile-native",
            "device.key",
            {"device_serial": "emulator-5554", "key": "enter"},
        )

        self.assertEqual(center.effect, "unknown")
        self.assertEqual(enter.effect, "unknown")
        self.assertEqual(
            self.calls(),
            [
                [
                    "key",
                    "--platform",
                    "android",
                    "--device",
                    "emulator-5554",
                    "KEYCODE_DPAD_CENTER",
                ],
                ["key", "--platform", "android", "--device", "emulator-5554", "enter"],
            ],
        )

    def test_raw_and_numeric_keycodes_are_rejected_before_dispatch(self) -> None:
        for key in (
            "KEYCODE_DPAD_CENTER",
            "KEYCODE_ENTER",
            "KEYCODE_HOME",
            "19",
            "23",
        ):
            with self.subTest(key=key):
                result = self.call(
                    "mobile-native",
                    "device.key",
                    {"device_serial": "emulator-5554", "key": key},
                )

                self.assertEqual(result.transport, "not_started")
                self.assertEqual(result.effect, "not_dispatched")
                self.assertEqual(result.error, "invalid_arguments")
                self.assertEqual(self.calls(), [])

    def test_visible_and_gone_assertions_return_structured_observations(self) -> None:
        tree = _ui_xml(
            _ui_node_xml(
                "android.widget.Button",
                text="Continue",
                resource_id="login:continue",
                clickable=True,
            )
        )
        visible = self.call(
            "mobile-native",
            "device.assert-visible",
            {"device_serial": "emulator-5554", "text": "TINUE"},
            mode="stdout",
            stdout=tree,
        )
        gone = self.call(
            "mobile-native",
            "device.assert-gone",
            {"device_serial": "emulator-5554", "text": "Missing"},
            mode="stdout",
            stdout=tree,
        )

        self.assertIsNone(visible.error)
        self.assertTrue(visible.data["visible"])
        self.assertIsNone(gone.error)
        self.assertFalse(gone.data["visible"])
        self.assertEqual(self.calls()[0][4], "xml")

    def test_assert_gone_rejects_native_error_envelope(self) -> None:
        result = self.call(
            "mobile-native",
            "device.assert-gone",
            {"device_serial": "emulator-5554", "text": "Unavailable"},
            mode="stdout",
            stdout='{"error":"device unavailable"}',
        )

        self.assertEqual(result.error, "invalid_provider_output")
        self.assertIsNone(result.data)

    def test_resource_id_assertions_require_exact_case_sensitive_match(self) -> None:
        tree = _ui_xml(
            _ui_node_xml(
                "android.widget.Button",
                resource_id="app:id/payment_history",
                clickable=True,
            )
        )
        prefix = self.call(
            "mobile-native",
            "device.assert-gone",
            {"device_serial": "emulator-5554", "resource_id": "app:id/pay"},
            mode="stdout",
            stdout=tree,
        )
        case_mismatch = self.call(
            "mobile-native",
            "device.assert-gone",
            {"device_serial": "emulator-5554", "resource_id": "APP:id/payment_history"},
            mode="stdout",
            stdout=tree,
        )
        exact = self.call(
            "mobile-native",
            "device.assert-visible",
            {"device_serial": "emulator-5554", "resource_id": "app:id/payment_history"},
            mode="stdout",
            stdout=tree,
        )

        self.assertIsNone(prefix.error)
        self.assertFalse(prefix.data["visible"])
        self.assertIsNone(case_mismatch.error)
        self.assertFalse(case_mismatch.data["visible"])
        self.assertIsNone(exact.error)
        self.assertTrue(exact.data["visible"])

    def test_native_tree_schema_is_projected_and_editable_text_is_redacted(self) -> None:
        private_value = "synthetic-private-value"
        result = self.call(
            "mobile-native",
            "device.tree",
            {"device_serial": "emulator-5554"},
            mode="stdout",
            stdout=_ui_xml(
                _ui_node_xml(
                    "android.widget.EditText",
                    text=private_value,
                    content_desc=private_value,
                    resource_id="app:id/password_input",
                    clickable=True,
                    password=True,
                )
            ),
        )

        self.assertIsNone(result.error)
        self.assertEqual(result.data[0]["text"], "<redacted>")
        self.assertEqual(result.data[0]["content_desc"], "<redacted>")
        self.assertTrue(result.data[0]["clickable"])
        self.assertTrue(result.data[0]["password"])
        self.assertNotIn(private_value, json.dumps(result.to_dict()))
        self.assertEqual(self.calls()[0][4], "xml")

    def test_text_assertions_treat_redacted_values_as_unknown(self) -> None:
        tree = _ui_xml(
            _ui_node_xml(
                "android.widget.EditText",
                text="Search",
                password=False,
            ),
            _ui_node_xml(
                "android.widget.Button",
                text="Continue",
                clickable=True,
            ),
        )
        gone = self.call(
            "mobile-native",
            "device.assert-gone",
            {"device_serial": "emulator-5554", "text": "Search"},
            mode="stdout",
            stdout=tree,
        )
        placeholder = self.call(
            "mobile-native",
            "device.assert-visible",
            {"device_serial": "emulator-5554", "text": "<redacted>"},
            mode="stdout",
            stdout=tree,
        )
        visible = self.call(
            "mobile-native",
            "device.assert-visible",
            {"device_serial": "emulator-5554", "text": "TINUE"},
            mode="stdout",
            stdout=tree,
        )

        for result in (gone, placeholder):
            self.assertEqual(result.error, "assertion_failed")
            self.assertEqual(result.data["state"], "unknown")
            self.assertNotIn("visible", result.data)
        self.assertIsNone(visible.error)
        self.assertTrue(visible.data["visible"])

    def test_text_assertions_treat_regex_redacted_values_as_unknown(self) -> None:
        node = _ui_node_xml(
            "android.widget.TextView",
            text="token=synthetic-private-value",
        )
        tree = _ui_xml(node)
        projected_tree = self.call(
            "mobile-native",
            "device.tree",
            {"device_serial": "emulator-5554"},
            mode="stdout",
            stdout=tree,
        )
        gone = self.call(
            "mobile-native",
            "device.assert-gone",
            {"device_serial": "emulator-5554", "text": "synthetic-private-value"},
            mode="stdout",
            stdout=tree,
        )
        placeholder = self.call(
            "mobile-native",
            "device.assert-visible",
            {"device_serial": "emulator-5554", "text": "<redacted>"},
            mode="stdout",
            stdout=tree,
        )

        self.assertIsNone(projected_tree.error)
        self.assertEqual(projected_tree.data[0]["text"], "<redacted>")
        for result in (gone, placeholder):
            self.assertEqual(result.error, "assertion_failed")
            self.assertEqual(result.data["state"], "unknown")
            self.assertNotIn("visible", result.data)

    def test_native_tree_accepts_and_preserves_naf_attribute(self) -> None:
        result = self.call(
            "mobile-native",
            "device.tree",
            {"device_serial": "emulator-5554"},
            mode="stdout",
            stdout=_ui_xml(_ui_node_xml("android.view.View", naf=True)),
        )

        self.assertIsNone(result.error)
        self.assertTrue(result.data[0]["naf"])

    def test_native_tree_ignores_unknown_metadata_and_preserves_privacy(self) -> None:
        result = self.call(
            "mobile-native",
            "device.tree",
            {"device_serial": "emulator-5554"},
            mode="stdout",
            stdout=_ui_xml(
                _ui_node_xml(
                    "android.widget.EditText",
                    text="private",
                    password=True,
                    extra_attribute='future-attribute="unsupported"',
                )
            ),
        )

        self.assertIsNone(result.error)
        self.assertEqual(result.data[0]["text"], "<redacted>")
        self.assertNotIn("future-attribute", result.data[0])

    def test_native_tree_rejects_xml_entities(self) -> None:
        result = self.call(
            "mobile-native",
            "device.tree",
            {"device_serial": "emulator-5554"},
            mode="stdout",
            stdout='<!DOCTYPE hierarchy [<!ENTITY private "secret">]>'
            '<hierarchy rotation="0"/>',
        )

        self.assertEqual(result.error, "invalid_provider_output")

    def test_failed_assertion_is_not_confused_with_transport_failure(self) -> None:
        result = self.call(
            "mobile-native",
            "device.assert-visible",
            {"device_serial": "emulator-5554", "text": "Missing"},
            mode="stdout",
            stdout=_ui_xml(),
        )

        self.assertEqual(result.error, "assertion_failed")
        self.assertEqual(result.transport, "completed")
        self.assertEqual(result.data["visible"], False)

    def test_sigterm_cleanup_uncertainty_is_not_reported_as_interrupted(self) -> None:
        class TerminateOnEnter:
            def __enter__(self):
                raise NATIVE._TerminationRequested()

            def __exit__(self, _type, _value, _traceback):
                return False

        settle_group = NATIVE._settle_group

        def settle_then_report_unknown(process, deadline):
            settle_group(process, deadline)
            return False

        with (
            patch.object(NATIVE, "_termination_guard", return_value=TerminateOnEnter()),
            patch.object(NATIVE, "_settle_group", side_effect=settle_then_report_unknown),
        ):
            result = NATIVE._run_process(
                (sys.executable, "-c", "import time; time.sleep(30)"),
                timeout_s=5.0,
                output_limit_bytes=1024,
            )

        self.assertEqual(result.error, "drain_unknown")

    def test_keyboard_interrupt_with_uncertain_cleanup_preserves_drain_unknown(self) -> None:
        class RaiseOnEnter:
            def __enter__(self):
                raise KeyboardInterrupt()

            def __exit__(self, _type, _value, _traceback):
                return False

        settle_group = NATIVE._settle_group

        def settle_then_report_unknown(process, deadline):
            settle_group(process, deadline)
            return False

        with (
            patch.object(NATIVE, "_termination_guard", return_value=RaiseOnEnter()),
            patch.object(NATIVE, "_settle_group", side_effect=settle_then_report_unknown),
        ):
            result = NATIVE._run_process(
                (sys.executable, "-c", "import time; time.sleep(30)"),
                timeout_s=5.0,
                output_limit_bytes=1024,
            )

        self.assertEqual(result.error, "drain_unknown")

    def test_preview_render_uses_explicit_root_target_and_fqn_without_daemon(self) -> None:
        result = self.call(
            "google-android",
            "preview.render",
            {"target": "app@devDebug", "fqn": "com.example.LoginPreview"},
            mode="preview",
            source_root=self.root,
            staging_dir=self.stage,
        )

        self.assertIsNone(result.error)
        self.assertEqual(len(result.artifacts), 2)
        artifact = result.artifacts[0]
        self.assertEqual((artifact["width"], artifact["height"]), (2, 3))
        self.assertEqual(artifact["sha256"], hashlib.sha256(PNG).hexdigest())
        tree_artifact = result.artifacts[1]
        safe_tree = Path(tree_artifact["path"]).read_bytes()
        self.assertNotIn(b"private-tree-marker", safe_tree)
        self.assertIn(b"<redacted>", safe_tree)
        self.assertEqual(tree_artifact["sha256"], hashlib.sha256(safe_tree).hexdigest())
        command = self.calls()[0]
        self.assertEqual(command[0:2], ["--no-metrics", "preview"])
        self.assertIn(":app@devDebug", command)
        self.assertIn("--fqn=com.example.LoginPreview", command)
        self.assertIn("--component-tree", command)
        self.assertIn("--configuration-cache", command)
        self.assertIn("--build-cache", command)
        self.assertIn("--parallel", command)
        self.assertNotIn("--daemon", command)
        self.assertIn("--output=" + str(self.stage.resolve()), command)
        for directory in Path(artifact["path"]).parents:
            if directory == self.stage.resolve().parent:
                break
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(Path(artifact["path"]).stat().st_mode), 0o600)

    def test_preview_component_tree_accepts_bounded_depth_34(self) -> None:
        result = self.call(
            "google-android",
            "preview.render",
            {"target": "app@devDebug", "fqn": "com.example.LoginPreview"},
            mode="preview-deep",
            source_root=self.root,
            staging_dir=self.stage,
        )

        self.assertIsNone(result.error)
        self.assertEqual(result.transport, "completed")
        self.assertEqual(len(result.artifacts), 2)

    def test_preview_component_tree_rejects_depth_above_bound(self) -> None:
        result = self.call(
            "google-android",
            "preview.render",
            {"target": "app@devDebug", "fqn": "com.example.LoginPreview"},
            mode="preview-too-deep",
            source_root=self.root,
            staging_dir=self.stage,
        )

        self.assertEqual(result.transport, "completed")
        self.assertEqual(result.error, "observation_limit")
        self.assertEqual(result.artifacts, ())

    def test_placeholder_preview_target_is_not_replaced_with_a_default(self) -> None:
        result = self.call(
            "google-android",
            "preview.list",
            {"target": "<library-target-missing>"},
            source_root=self.root,
        )

        self.assertEqual(result.error, "unsupported_target")
        self.assertEqual(result.effect, "not_dispatched")
        self.assertEqual(self.calls(), [])

    def test_preview_list_uses_explicit_target_and_includes_dependencies(self) -> None:
        result = self.call(
            "google-android",
            "preview.list",
            {"target": "app@devDebug"},
            mode="stdout",
            stdout='{"previews":[]}',
            source_root=self.root,
        )

        self.assertIsNone(result.error)
        command = self.calls()[0]
        self.assertEqual(
            command,
            [
                "--no-metrics",
                "preview",
                "list",
                "--all",
                "--format=json",
                ":app@devDebug",
                "--",
                "--configuration-cache",
                "--build-cache",
                "--parallel",
            ],
        )

    def test_worktree_preview_is_rejected_without_build_launcher(self) -> None:
        worktree = self.root / "worktree"
        worktree.mkdir()
        (worktree / ".git").write_text("gitdir: /private/metadata\n")

        result = self.call(
            "google-android",
            "preview.list",
            {"target": "app@devDebug"},
            source_root=worktree,
        )

        self.assertEqual(result.error, "unsupported_build_launcher")
        self.assertEqual(self.calls(), [])

    def test_preview_rejects_arbitrary_build_args_and_requires_root(self) -> None:
        arguments_result = self.call(
            "google-android",
            "preview.list",
            {"target": "app@devDebug", "build_args": ["--scan"]},
            source_root=self.root,
        )
        root_result = self.call(
            "google-android",
            "preview.list",
            {"target": "app@devDebug"},
        )

        self.assertEqual(arguments_result.error, "invalid_arguments")
        self.assertEqual(root_result.error, "source_root_required")
        self.assertEqual(self.calls(), [])

    def test_preview_rejects_provider_paths_outside_private_staging(self) -> None:
        result = self.call(
            "google-android",
            "preview.render",
            {"target": "app@devDebug", "fqn": "com.example.LoginPreview"},
            mode="preview-traversal",
            source_root=self.root,
            staging_dir=self.stage,
        )

        self.assertEqual(result.error, "invalid_artifact_path")

    def test_artifact_path_traversal_is_rejected_before_dispatch(self) -> None:
        result = self.call(
            "mobile-native",
            "device.capture",
            {"device_serial": "emulator-5554", "output_name": "../outside.png"},
            staging_dir=self.stage,
        )

        self.assertEqual(result.error, "invalid_output_path")
        self.assertEqual(self.calls(), [])
        self.assertTrue(self.outside.exists())

    def test_capture_validates_png_and_returns_hash_metadata(self) -> None:
        result = self.call(
            "mobile-native",
            "device.capture",
            {"device_serial": "emulator-5554"},
            mode="png",
            staging_dir=self.stage,
        )

        self.assertIsNone(result.error)
        self.assertEqual(result.artifacts[0]["media_type"], "image/png")
        self.assertEqual(result.artifacts[0]["sha256"], hashlib.sha256(PNG).hexdigest())

    def test_capture_rejects_symlink_output(self) -> None:
        result = self.call(
            "mobile-native",
            "device.capture",
            {"device_serial": "emulator-5554"},
            mode="symlink",
            staging_dir=self.stage,
        )

        self.assertEqual(result.error, "invalid_artifact_path")

    def test_capture_rejects_invalid_compressed_png_pixels(self) -> None:
        result = self.call(
            "mobile-native",
            "device.capture",
            {"device_serial": "emulator-5554"},
            mode="png-invalid",
            staging_dir=self.stage,
        )

        self.assertEqual(result.error, "invalid_png")

    def test_missing_capture_output_is_categorical(self) -> None:
        result = self.call(
            "mobile-native",
            "device.capture",
            {"device_serial": "emulator-5554"},
            staging_dir=self.stage,
        )

        self.assertEqual(result.error, "artifact_missing")


if __name__ == "__main__":
    unittest.main()
