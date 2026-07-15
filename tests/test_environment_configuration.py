import ast
import re
import unittest
from pathlib import Path

from core.runtime_environment import CONFIGURED_ENVIRONMENT_KEYS

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENVIRONMENT_ENTRY_PATTERN = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")
DEPLOYED_ENVIRONMENT_PATTERN = re.compile(r'^\s{12}([A-Z][A-Z0-9_]*)\s+"\$\1"')


def get_sample_environment_keys() -> list[str]:
    """Sample envに定義された環境変数名を順序付きで返す。"""
    lines = (PROJECT_ROOT / ".env.sample").read_text(encoding="utf-8").splitlines()
    return [match.group(1) for line in lines if (match := ENVIRONMENT_ENTRY_PATTERN.match(line))]


def get_deployed_environment_keys() -> list[str]:
    """GCPコンテナ用envファイルへ書き込む環境変数名を返す。"""
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "deployment.yml").read_text(encoding="utf-8")
    return [match.group(1) for line in workflow.splitlines() if (match := DEPLOYED_ENVIRONMENT_PATTERN.match(line))]


def get_python_environment_keys() -> set[str]:
    """Pythonコードがos.environまたはos.getenvで直接参照する変数名を返す。"""
    keys: set[str] = set()
    for path in PROJECT_ROOT.rglob("*.py"):
        if ".venv" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Subscript) and _is_os_environ(node.value):
                if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
                    keys.add(node.slice.value)
            elif (
                isinstance(node, ast.Call)
                and node.args
                and (_is_os_getenv(node.func) or _is_os_environ_get(node.func))
                and isinstance(node.args[0], ast.Constant)
            ):
                key = node.args[0].value
                if isinstance(key, str):
                    keys.add(key)
    return keys


def _is_os_environ(node: ast.expr) -> bool:
    """AST要素がos.environを表すか判定する。"""
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
        and node.attr == "environ"
    )


def _is_os_getenv(node: ast.expr) -> bool:
    """AST要素がos.getenvを表すか判定する。"""
    return (
        isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "os" and node.attr == "getenv"
    )


def _is_os_environ_get(node: ast.expr) -> bool:
    """AST要素がos.environ.getを表すか判定する。"""
    return isinstance(node, ast.Attribute) and node.attr == "get" and _is_os_environ(node.value)


def ensure(condition: object, message: str = "") -> None:
    """条件を満たさない場合にテストを失敗させる。"""
    if not condition:
        raise AssertionError(message)


class EnvironmentConfigurationTest(unittest.TestCase):
    def test_sample_matches_validated_environment_keys(self) -> None:
        sample_keys = get_sample_environment_keys()

        ensure(len(sample_keys) == len(set(sample_keys)), ".env.sampleに重複した環境変数があります")
        ensure(set(sample_keys) == set(CONFIGURED_ENVIRONMENT_KEYS))

    def test_deployment_passes_every_sample_environment_key_once(self) -> None:
        sample_keys = get_sample_environment_keys()
        deployed_keys = get_deployed_environment_keys()

        ensure(len(deployed_keys) == len(set(deployed_keys)), "deploymentに重複した環境変数があります")
        ensure(set(deployed_keys) == set(sample_keys))

    def test_python_environment_access_is_declared(self) -> None:
        undeclared_keys = get_python_environment_keys() - CONFIGURED_ENVIRONMENT_KEYS

        ensure(undeclared_keys == set())
