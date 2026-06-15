#!/usr/bin/env python3
"""
LLM wrapper for Phase 1 spec generation.
Supports both Ollama (local) and Claude API (remote).
"""

import json
import os
import requests
from typing import Optional, Callable


def _get_available_ollama_models(base_url: str = "http://localhost:11434") -> list:
    """Get list of available models from Ollama."""
    try:
        url = f"{base_url}/api/tags"
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        data = response.json()
        models = [m["name"] for m in data.get("models", [])]
        return models
    except Exception:
        return []


def _select_best_ollama_model(base_url: str = "http://localhost:11434") -> str:
    """Auto-select the best available model for spec generation."""
    available = _get_available_ollama_models(base_url)

    # Preferred models in order (balance quality vs speed)
    preferred = [
        "gemma4:12b",     # Primary — user-downloaded 12B model
        "gemma4:e2b",     # Efficient variant, 7.2 GB
        "gemma4:latest",  # Good quality, 9.6 GB
        "qwen3.5",        # Good for structured output
        "qwen",
        "neural-chat",
        "llama3",
        "mistral",
        "llama2",
        "llama3.2",       # Small, JSON can be truncated
    ]

    for model in preferred:
        for avail in available:
            if model in avail or avail.startswith(model):
                return avail

    # Fallback to first available
    if available:
        return available[0]

    raise RuntimeError("No Ollama models found. Run: ollama pull llama3.2")


def create_ollama_llm(
    model: str = None,
    base_url: str = "http://localhost:11434"
) -> Callable[[str, float], str]:
    """Create an LLM function that calls Ollama locally.

    Args:
        model: Ollama model name (e.g., 'llama3.2', 'mistral').
               If None, auto-selects the best available model.
        base_url: Ollama server URL (default: localhost:11434)

    Returns:
        Function that takes (prompt, temperature) and returns JSON string.
    """
    # Auto-select model if not specified
    if model is None:
        model = _select_best_ollama_model(base_url)
        print(f"Auto-selected Ollama model: {model}")

    def llm_fn(prompt: str, temperature: float = 0) -> str:
        """Call Ollama and return raw response text."""
        url = f"{base_url}/api/generate"
        payload = {
            "model": model,
            "prompt": prompt,
            "temperature": temperature,
            "stream": False,
        }

        try:
            response = requests.post(url, json=payload, timeout=600)  # 10 min for slow models
            response.raise_for_status()
            result = response.json()
            return result.get("response", "")
        except requests.exceptions.ConnectionError:
            raise ConnectionError(
                f"Could not connect to Ollama at {base_url}. "
                "Make sure Ollama is running: ollama serve"
            )
        except Exception as e:
            raise RuntimeError(f"Ollama request failed: {e}")

    return llm_fn


def create_claude_llm(api_key: Optional[str] = None) -> Callable[[str, float], str]:
    """Create an LLM function that calls Claude API (requires paid account).

    Args:
        api_key: Claude API key. If None, reads from ANTHROPIC_API_KEY env var.

    Returns:
        Function that takes (prompt, temperature) and returns JSON string.
    """
    import anthropic

    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError(
            "Claude API key not provided. Set ANTHROPIC_API_KEY env var or pass api_key parameter."
        )

    client = anthropic.Anthropic(api_key=api_key)

    def llm_fn(prompt: str, temperature: float = 0) -> str:
        """Call Claude API and return raw response text."""
        message = client.messages.create(
            model="claude-opus-4-8",
            max_tokens=1024,
            temperature=temperature,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text

    return llm_fn


if __name__ == "__main__":
    # Test the wrapper
    import sys

    print("Testing LLM connection...")
    print()

    # Try Ollama first
    try:
        print("1. Checking Ollama (localhost:11434)...")
        llm = create_ollama_llm(model="mistral")
        test_result = llm("Respond with: OK", temperature=0)
        if "OK" in test_result:
            print("   ✓ Ollama connection successful")
            print(f"   Model: mistral")
            sys.exit(0)
    except ConnectionError as e:
        print(f"   ✗ {e}")
    except Exception as e:
        print(f"   ✗ Ollama error: {e}")

    print()
    print("2. Checking Claude API...")
    try:
        llm = create_claude_llm()
        print("   ✓ Claude API connection successful")
        sys.exit(0)
    except Exception as e:
        print(f"   ✗ Claude API: {e}")

    print()
    print("✗ No LLM available. Install Ollama or set ANTHROPIC_API_KEY env var.")
    sys.exit(1)
