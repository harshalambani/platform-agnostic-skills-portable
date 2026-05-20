"""
base_agent.py — shared model loader and LangGraph agent builder.
All three skills import this. To switch models, edit config.yaml or pass
model_override at runtime (used by the --model CLI flag).
"""
import yaml
from pathlib import Path
from langgraph.prebuilt import create_react_agent


def load_model(config_path: str = "config.yaml", model_override: str = None):
    """
    Load the LLM specified in config.yaml.

    Args:
        config_path:    Path to config.yaml.
        model_override: If set, overrides the default_model in config.yaml.
                        Pass the model name exactly as Ollama/vLLM knows it
                        (e.g. 'llama3.1', 'qwen3', 'phi4-mini').
    """
    cfg = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
    provider = cfg["provider"]

    if provider == "ollama":
        from langchain_ollama import ChatOllama
        oc = cfg["ollama"]
        # Resolve model: CLI override > config default_model
        model_name = model_override or oc.get("default_model", "llama3.1")
        print(f"[base_agent] Using Ollama model: {model_name}")
        return ChatOllama(
            base_url=oc["base_url"],
            model=model_name,
            temperature=oc["temperature"],
        )

    elif provider == "openai_compatible":
        from langchain_openai import ChatOpenAI
        oc = cfg["openai_compatible"]
        model_name = model_override or oc.get("default_model", "gemma-4")
        print(f"[base_agent] Using OpenAI-compatible model: {model_name}")
        return ChatOpenAI(
            base_url=oc["base_url"],
            api_key=oc["api_key"],
            model=model_name,
            temperature=oc["temperature"],
        )

    else:
        raise ValueError(
            f"Unknown provider '{provider}' in config.yaml. "
            "Use 'ollama' or 'openai_compatible'."
        )


def build_agent(
    tools: list,
    system_prompt: str,
    config_path: str = "config.yaml",
    model_override: str = None,
):
    """
    Build a LangGraph ReAct agent with the given tools and system prompt.

    Args:
        tools:          List of @tool-decorated functions.
        system_prompt:  The agent's system prompt (loaded from AGENT.md).
        config_path:    Path to config.yaml.
        model_override: Optional model name to use instead of config default.
                        e.g. 'llama3.1', 'qwen3', 'phi4-mini'
    """
    model = load_model(config_path, model_override)

    # 'prompt' replaced 'state_modifier' in LangGraph >= 0.2.x
    try:
        return create_react_agent(
            model=model,
            tools=tools,
            prompt=system_prompt,
        )
    except TypeError:
        # Fallback for older LangGraph installs
        return create_react_agent(
            model=model,
            tools=tools,
            state_modifier=system_prompt,
        )
