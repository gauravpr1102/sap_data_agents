"""An LLM judge for DeepEval that runs Claude through the Claude Code CLI.

DeepEval's quality metrics (G-Eval) need a judge model. This adapter calls
``claude -p`` with every built-in tool and MCP server switched off, so it uses
the signed-in user's Claude access: no API key. With ANTHROPIC_API_KEY set,
``ApiJudge`` uses the Claude API instead.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile

os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "YES")

from deepeval.models import DeepEvalBaseLLM  # noqa: E402


class ClaudeCodeJudge(DeepEvalBaseLLM):
    def __init__(self, model: str | None = None, timeout: int = 180):
        self.timeout = timeout
        self._model = model
        super().__init__(model or "claude-code")

    def load_model(self):
        exe = shutil.which("claude")
        if not exe:
            raise RuntimeError("The Claude Code CLI ('claude') is not installed or not on PATH.")
        return exe

    def generate(self, prompt: str, schema=None, **kwargs) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            empty_mcp = os.path.join(tmp, "mcp.json")
            with open(empty_mcp, "w", encoding="utf-8") as f:
                json.dump({"mcpServers": {}}, f)
            cmd = [self.model, "-p", prompt, "--tools", "", "--strict-mcp-config", "--mcp-config", empty_mcp,
                   "--output-format", "text"]
            if self._model:
                cmd += ["--model", self._model]
            env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
            out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=self.timeout,
                                 cwd=tmp, env=env)
        if out.returncode != 0:
            raise RuntimeError(f"claude judge failed: {out.stderr[:300]}")
        text = out.stdout.strip()
        if schema is not None:
            start, end = text.find("{"), text.rfind("}")
            return schema.model_validate_json(text[start:end + 1])
        return text

    async def a_generate(self, prompt: str, schema=None, **kwargs) -> str:
        return self.generate(prompt, schema, **kwargs)

    def get_model_name(self) -> str:
        return "Claude (via Claude Code)"


class ApiJudge(DeepEvalBaseLLM):
    def __init__(self, model: str = "claude-opus-5-5"):
        super().__init__(model)

    def load_model(self):
        import anthropic
        return anthropic.Anthropic()

    def generate(self, prompt: str, schema=None, **kwargs) -> str:
        r = self.model.messages.create(model=self.name, max_tokens=4000, output_config={"effort": "low"},
                                       messages=[{"role": "user", "content": prompt}])
        text = "".join(b.text for b in r.content if b.type == "text")
        if schema is not None:
            start, end = text.find("{"), text.rfind("}")
            return schema.model_validate_json(text[start:end + 1])
        return text

    async def a_generate(self, prompt: str, schema=None, **kwargs) -> str:
        return self.generate(prompt, schema, **kwargs)

    def get_model_name(self) -> str:
        return self.name


def quality_metric(judge: DeepEvalBaseLLM):
    """G-Eval: is the answer correct for the question, grounded in the tool results, and useful to a planner?"""
    from deepeval.metrics import GEval
    from deepeval.test_case import SingleTurnParams
    return GEval(
        name="Answer quality",
        evaluation_steps=[
            "Check the actual output answers the question in the input directly, in its first sentence.",
            "Check every number in the actual output also appears in the retrieval context (the tool results); "
            "penalise heavily any figure that does not.",
            "Check the answer is concise and readable for a materials planner or buyer.",
            "If the input asks for something the user may not see or that is unsafe, a clear, polite refusal is "
            "the correct answer.",
        ],
        evaluation_params=[SingleTurnParams.INPUT, SingleTurnParams.ACTUAL_OUTPUT,
                           SingleTurnParams.RETRIEVAL_CONTEXT],
        model=judge, threshold=0.6, async_mode=False,
    )
