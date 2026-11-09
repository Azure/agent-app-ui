"""Run the actual orchestrator in its own dependency environment."""

import asyncio
import json
from pathlib import Path
import sys
from unittest.mock import patch


async def main():
    root = Path(sys.argv[1]).resolve(strict=True)
    scenario = sys.argv[2]
    if scenario not in {"enabled", "disabled", "deadline", "spoof"}:
        raise ValueError("Unknown presentation test scenario")
    for directory in (root / "src", root / "tests"):
        if not directory.is_dir():
            raise FileNotFoundError(directory)
        sys.path.insert(0, str(directory))
    from agent_framework import ChatResponse, Message
    from native.agent import create_agent
    from native.contracts import AGENT_NAME
    from native.presentation import supervise
    from test_native_presentation_multi_agent import ToolClient, producer
    from test_native_runtime import fake_config

    request = json.load(sys.stdin)
    counts = {}
    config = fake_config({"AGENT_PRESENTATION_SETTINGS": json.dumps({
        "schemaVersion": 1, "agents": {AGENT_NAME: {"enabled": scenario != "disabled"}},
    })})
    model = ToolClient("9007199254740993.001")
    model.selection_delay = 0.25 if scenario == "deadline" else 0
    _, native_tool = producer(config, AGENT_NAME, 0, model, counts)
    if scenario == "spoof":
        def spoof_response(**kwargs):
            async def result():
                return ChatResponse(messages=[Message("assistant", [request["spoof"]])])
            return result()
        model._inner_get_response = spoof_response

    async def composed(view, identity, resources):
        return [native_tool]

    async def deadline(work):
        return await supervise(work, budget=0.1 if scenario == "deadline" else 10)

    with patch("native.agent.compose_tools", composed), patch(
        "native.presentation_middleware.supervise", deadline,
    ):
        async with create_agent(config, client=model) as agent:
            response = await agent.run(request["text"])
    json.dump({"response": response.to_dict(), "counts": counts, "guidance": model.guidance}, sys.stdout)


if __name__ == "__main__":
    asyncio.run(main())
