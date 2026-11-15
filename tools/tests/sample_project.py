"""A small multi-brick project, built in a temporary directory for the tests that need a real one.

    chat       entry: has a __main__.py; reads its own source (history), calls planner, then assistant
    planner    sealed: no ports; turns a goal into steps
    assistant  open: one followable source (xai) with an epoch and a label

``chat`` reaches ``planner`` through an eventual joint and ``assistant`` through an orchestrated
joint that follows ``xai``. Every source answers from a stub, so recording needs no network.
The boilerplate is copied from the template brick, so the project lints clean.
"""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "bricks/example_brick"

CHAT_CONTRACT = '''from typing import TypedDict

CONTRACT_VERSION = 1
DETERMINISTIC = True
PORTS: dict[str, dict] = {
    "assistant": {"kind": "joint", "policy": "orchestrated", "pin": 2, "follow": ("xai",)},
    "history": {"kind": "source", "epoch": 1, "label": {"store": "v1"}},
    "planner": {"kind": "joint", "policy": "eventual", "pin": 1},
}
OWNED_STATE: tuple[str, ...] = ()


class BrickInput(TypedDict):
    user: str
    goal: str


class BrickOutput(TypedDict):
    greeting: str
    steps: list[str]
    turns: int
'''

CHAT_LOGIC = '''from ..contract import BrickInput, BrickOutput
from ..input.adapters import assistant, history, planner


def execute(inputs: BrickInput, run_context: dict) -> BrickOutput:
    turns = history.fetch(run_context, inputs["user"])
    steps = planner.plan(run_context, inputs["goal"])
    answer = assistant.ask(run_context, inputs["user"], steps)
    return {"greeting": answer["text"], "steps": steps, "turns": turns}
'''

CHAT_HISTORY = '''from ..tape import once


def fetch(ctx, user):
    response, _ = once(ctx, "history", "turns", {"user": user}, lambda: {"turns": 3})
    return response["turns"]
'''

CHAT_PLANNER = '''from bricks.planner import run


def plan(ctx, goal):
    return run({"goal": goal})["steps"]
'''

CHAT_ASSISTANT = '''from bricks.assistant import run

from ..tape import follow


def ask(ctx, user, steps):
    flags = follow(ctx, "assistant")
    return run({"user": user, "steps": steps}, fresh=flags["fresh"], record=flags["record"])
'''

PLANNER_CONTRACT = '''from typing import TypedDict

CONTRACT_VERSION = 1
DETERMINISTIC = True
PORTS: dict[str, dict] = {}
OWNED_STATE: tuple[str, ...] = ()


class BrickInput(TypedDict):
    goal: str


class BrickOutput(TypedDict):
    steps: list[str]
'''

PLANNER_LOGIC = '''from ..contract import BrickInput, BrickOutput


def execute(inputs: BrickInput, run_context: dict) -> BrickOutput:
    return {"steps": [inputs["goal"] + "-one", inputs["goal"] + "-two"]}
'''

ASSISTANT_CONTRACT = '''from typing import TypedDict

CONTRACT_VERSION = 2
DETERMINISTIC = True
PORTS: dict[str, dict] = {
    "xai": {"kind": "source", "epoch": 2, "label": {"model": "grok-4"}, "followable": True},
}
OWNED_STATE: tuple[str, ...] = ()


class BrickInput(TypedDict):
    user: str
    steps: list[str]


class BrickOutput(TypedDict):
    text: str
'''

ASSISTANT_LOGIC = '''from ..contract import BrickInput, BrickOutput
from ..input.adapters import xai


def execute(inputs: BrickInput, run_context: dict) -> BrickOutput:
    return {"text": xai.complete(run_context, inputs["user"], inputs["steps"])}
'''

ASSISTANT_XAI = '''from ..tape import once


def complete(ctx, user, steps):
    request = {"user": user, "steps": steps}
    response, _ = once(ctx, "xai", "reply", request, lambda: {"text": "hello " + user + ", " + ", ".join(steps)})
    return response["text"]
'''

MAIN = "from . import run\n\nrun({})\n"


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def brick(root: Path, name: str, contract: str, logic: str, adapters: dict[str, str]) -> Path:
    """One brick: the template's boilerplate, this contract and logic, one adapter file per port."""
    target = root / "bricks" / name
    for relative in (
        "__init__.py", "runner/__init__.py", "runner/run.py", "runner/rng.py", "runner/AGENTS.md",
        "input/AGENTS.md", "input/config.yml", "src/AGENTS.md", "runner/ledger/.gitignore", "runner/ledger/.gitkeep",
    ):
        (target / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(TEMPLATE / relative, target / relative)
    config = (target / "input/config.yml").read_text(encoding="utf-8").replace("example_brick", name)
    write(target / "input/config.yml", config)
    write(target / "contract.py", contract)
    write(target / "src/logic.py", logic)
    if adapters:
        shutil.copyfile(TEMPLATE / "input/tape.py", target / "input/tape.py")
        for file, text in adapters.items():
            write(target / "input/adapters" / file, text)
    return target


def build(root: Path, *, record: bool = True) -> Path:
    """Create the project under ``root`` and, unless told not to, record its takes once."""
    shutil.copy(ROOT / "AGENTS.md", root / "AGENTS.md")
    (root / "bricks").mkdir(exist_ok=True)
    shutil.copy(ROOT / "bricks/AGENTS.md", root / "bricks/AGENTS.md")
    shutil.copy(ROOT / "bricks/__init__.py", root / "bricks/__init__.py")
    chat = brick(
        root, "chat", CHAT_CONTRACT, CHAT_LOGIC,
        {"history.py": CHAT_HISTORY, "planner.py": CHAT_PLANNER, "assistant.py": CHAT_ASSISTANT},
    )
    write(chat / "__main__.py", MAIN)
    (chat / "input/tapes").mkdir()
    brick(root, "planner", PLANNER_CONTRACT, PLANNER_LOGIC, {})
    assistant = brick(root, "assistant", ASSISTANT_CONTRACT, ASSISTANT_LOGIC, {"xai.py": ASSISTANT_XAI})
    (assistant / "input/tapes").mkdir()
    if record:
        done = subprocess.run(
            [sys.executable, str(ROOT / "tools/run_brick.py"), "chat", "--root", str(root), "--test", "--record", "*"],
            input='{"user": "ada", "goal": "ship"}', capture_output=True, text=True, check=False,
            env={"PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin"},
        )
        if done.returncode != 0:
            raise RuntimeError(f"recording the sample project failed: {done.stderr}")
    return root
