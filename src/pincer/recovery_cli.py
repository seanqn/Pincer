"""Explicit continuation commands for a trusted local checkpoint database."""
import argparse
import asyncio
import json
from importlib import import_module
from pathlib import Path
from uuid import uuid4

from agents import set_trace_processors
from agents.tracing import trace

from .adapter import OpenAIAgentsTraceAdapter
from .runner import RecoveryProfile, recover_checkpoint, resume_breakpoint


def _load_profile(spec: str) -> RecoveryProfile:
    module, separator, factory = spec.partition(":")
    if not separator or not module or not factory:
        raise ValueError("Use --agent MODULE:FACTORY")
    profile = getattr(import_module(module), factory)()
    if not isinstance(profile, RecoveryProfile) or not profile.configuration_id:
        raise ValueError("The agent factory must return a RecoveryProfile with a configuration ID")
    return profile


async def _run(args: argparse.Namespace) -> None:
    profile = _load_profile(args.agent)
    directory = args.trace_dir if args.trace_dir is not None else args.database.resolve().parent
    processor = OpenAIAgentsTraceAdapter(directory / f"continued-{uuid4().hex}.jsonl")
    set_trace_processors([processor])
    print(f"Trace: {json.dumps(str(processor.path))}", flush=True)
    try:
        with trace(
            "Pincer manual continuation",
            disabled=profile.run_config.tracing_disabled if profile.run_config else False,
        ):
            continue_run = resume_breakpoint if args.approve else recover_checkpoint
            result = await continue_run(
                profile.agent, args.database,
                checkpoint_id=args.checkpoint, run_config=profile.run_config,
                configuration_id=profile.configuration_id, trace_adapter=processor,
            )
    finally:
        set_trace_processors([])
        processor.shutdown()

    if result.interruptions:
        print("Paused for tool approval. Inspect the checkpoint history before continuing.")
    else:
        print(result.final_output)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="pincer recover", description=__doc__)
    parser.add_argument("database", type=Path, help="checkpoint SQLite database")
    parser.add_argument("--agent", required=True, metavar="MODULE:FACTORY", help="trusted recovery profile factory")
    parser.add_argument("--checkpoint", type=int, metavar="ID", help="checkpoint ID, defaults to the current checkpoint")
    parser.add_argument("--approve", action="store_true", help="approve and dispatch the pending tool call")
    parser.add_argument("--trace-dir", type=Path, help="directory for the continuation trace")
    args = parser.parse_args(argv)
    try:
        asyncio.run(_run(args))
    except Exception as exc:
        parser.exit(1, f"pincer recover: {json.dumps(str(exc))}\n")
