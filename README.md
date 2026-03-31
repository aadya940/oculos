<p>
<img src="logo.png" align="center">
</p>

Orbit is a composable toolkit for building Computer Use Agents (CUAs). It provides both a standalone multi-step agent and a composable SDK.

Most CUA frameworks either automate the complete task as a black box or expose raw tools with no structure. Orbit sits in between , natural language controls the screen, Python controls the flow. Each primitive (`Do`, `Read`, `Check`, `Navigate`, `Fill`) is an independent agent with its own budget, model, and typed output, but they share context within a session. This means you can use a lightweight model for simple clicks and a heavier model for complex tasks, control max LLM calls per step, and extract structured data from the screen into Pydantic models.

Orbit uses the OS accessibility tree instead of screenshots or DOM parsing, which means less token usage and direct access to UI elements across both desktop apps and browsers.

## Standalone Agent

For one-shot tasks, just describe what you want:

```python
from orbit import Agent
import asyncio

async def main():
    result = await Agent(
        task="Open Chrome and navigate to Wikipedia",
        llm="gemini-3-pro-preview",
        verbose=True,
    ).run()
    print(result.status, result.summary)

asyncio.run(main())
```

## Composable SDK

For more controlled workflows, use verbs with a shared session in a pythonic way:

```python
from orbit import Do, Read, Check, Navigate, Fill, session
from pydantic import BaseModel
import asyncio

class Product(BaseModel):
    name: str
    price: float
    in_stock: bool

async def main():
    # Use lighter models for routine actions and a stronger one for extraction.
    action_model = "gemini-3.1-flash-lite-preview"
    extract_model = "gemini-3-pro-preview"

    async with session() as s:
        await Navigate(
            "amazon.com",
            session=s,
            llm=action_model,
            max_steps=30,
            verbose=True,
        ).run()
        await Do(
            "search for 'mechanical keyboard'",
            session=s,
            llm=action_model,
            max_steps=30,
            verbose=True,
        ).run()

        products = await Read(
            "the search results",
            schema=Product,
            session=s,
            llm=extract_model,
            max_steps=30,
            verbose=True,
        ).run()

        cheapest = min(products.output, key=lambda p: p.price)
        await Do(
            f"click on '{cheapest.name}'",
            session=s,
            llm=action_model,
            max_steps=30,
            verbose=True,
        ).run()

        if await Check(
            "Add to Cart button is visible",
            session=s,
            llm=action_model,
            max_steps=30,
            verbose=True,
        ).check():
            await Do(
                "click Add to Cart",
                session=s,
                llm=action_model,
                max_steps=30,
                verbose=True,
            ).run()

asyncio.run(main())
```

## Installation

```bash
git clone --recurse-submodules https://github.com/aadya940/orbit.git
cd orbit

# Build the OculOS daemon (requires Rust)
cd oculos && cargo build --release && cd ..
mkdir -p orbit/_bin
# Windows:
copy oculos\target\release\oculos.exe orbit\_bin\oculos.exe
# Linux/macOS:
# cp oculos/target/release/oculos orbit/_bin/oculos

pip install .
```

Set your API key for whichever provider you use. Orbit supports any model via [LiteLLM](https://docs.litellm.ai/):

```bash
# Gemini
export GEMINI_API_KEY="your-key"

# OpenAI
export OPENAI_API_KEY="your-key"

# Anthropic
export ANTHROPIC_API_KEY="your-key"
```

> `pip install orbit-agent` coming soon.

## Safety

Orbit never permanently deletes files , destructive operations go to Trash/Recycle Bin. Disk writes require human approval via a configurable callback.

## License

MIT
</content>
</invoke>
