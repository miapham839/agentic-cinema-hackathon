# Interactive Cinema Agent - Customization Guide

This project has been scaffolded with a **multi-agent architecture** following ADK best practices.

## 🏗️ Architecture

```
root_agent (orchestrator)
├── subagent_1  [CUSTOMIZE NAME/ROLE]
├── subagent_2  [CUSTOMIZE NAME/ROLE]
└── subagent_3  [CUSTOMIZE NAME/ROLE]
```

## 📝 What to Customize

### 1. **app/agent.py** - Agent Definitions

For each sub-agent and the root agent, replace the `[PLACEHOLDER]` sections with:

#### Sub-Agent Names
```python
# Replace:
name="subagent_1",

# With something descriptive:
name="content_analyzer",
name="recommendation_engine",
name="interaction_handler",
```

#### Sub-Agent Descriptions
```python
# Replace:
description="[PLACEHOLDER] Replace with clear description..."

# With:
description="Analyzes movie content and extracts themes, genres, and key information."
description="Provides personalized movie recommendations based on user preferences."
description="Handles user interactions and session management."
```

**⚠️ Important:** The `description` field is used by the root agent to decide when to delegate to each sub-agent. Be clear and specific!

#### Instructions
Replace the placeholder instructions with detailed guidance for each agent:
- What tasks they should handle
- How to use their tools
- Expected behavior and output format
- Any constraints or rules

### 2. **app/tools.py** - Tool Functions

Replace the placeholder tool functions with your actual implementations:

#### Custom Tools
```python
def subagent1_tool_placeholder(param1: str, param2: int, tool_context: ToolContext) -> dict:
    # TODO: Implement your actual tool logic
    # - Call external APIs
    # - Process data
    # - Query databases
    # - etc.
    pass
```

#### MCP Tools (Optional)
Uncomment and configure MCP tools in `tools.py`:

```python
# Local MCP server
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from mcp import StdioServerParameters

subagent1_mcp_tools = McpToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command="npx",
            args=["-y", "@modelcontextprotocol/server-filesystem", "/path"],
        ),
    ),
)
```

Then add to the agent:
```python
tools=[
    subagent1_tool_placeholder,
    subagent1_mcp_tools,  # Add MCP toolset
]
```

#### Built-in ADK Tools (Optional)
```python
from google.adk.tools import google_search
from google.adk.tools.load_web_page import load_web_page

# Add to agent's tools list:
tools=[google_search, load_web_page]
```

### 3. **Root Agent Instructions**

Update the root agent's instruction to describe:
- What each sub-agent handles
- When to delegate to which sub-agent
- How to coordinate multi-agent responses

```python
instruction="""You are the orchestrator for an interactive cinema experience.

Delegate tasks as follows:
- For content analysis → use content_analyzer
- For recommendations → use recommendation_engine
- For user interactions → use interaction_handler

Always choose the most appropriate sub-agent based on the user's request.
"""
```

## 🚀 Quick Start After Customization

### 1. Test Locally
```bash
cd interactive-cinema-agent

# Quick smoke test
agents-cli run "test your agent with a sample prompt"

# Interactive playground
agents-cli playground
```

### 2. Run Evaluation
```bash
# Create eval cases in tests/eval/datasets/
# Then run:
agents-cli eval run
```

### 3. Deploy (Optional)
```bash
# Add deployment target
agents-cli scaffold enhance . --deployment-target agent_runtime

# Deploy
agents-cli deploy
```

## 📚 Reference Documentation

See the `.adk-reference/` directory in the parent project for comprehensive guides:
- `MULTI-AGENT-PATTERNS.md` - Multi-agent best practices
- `BEST-PRACTICES.md` - ADK development guidelines
- `DEPLOYMENT-GUIDE.md` - Deployment workflows
- `EVALUATION-GUIDE.md` - Testing methodology

## 🔧 Common Customizations

### Add More Sub-Agents
```python
# In agent.py, add a new sub-agent:
subagent_4 = Agent(
    name="my_new_agent",
    description="...",
    instruction="...",
    tools=[...],
)

# Add to root agent:
root_agent = Agent(
    sub_agents=[subagent_1, subagent_2, subagent_3, subagent_4],
)
```

### Remove a Sub-Agent
Simply remove it from the `sub_agents` list:
```python
root_agent = Agent(
    sub_agents=[subagent_1, subagent_2],  # Removed subagent_3
)
```

### Add State Management
```python
from google.adk.agents.callback_context import CallbackContext

async def initialize_state(callback_context: CallbackContext) -> None:
    if "user_preferences" not in callback_context.state:
        callback_context.state["user_preferences"] = {}

root_agent = Agent(
    before_agent_callback=initialize_state,
    ...
)
```

## ⚠️ Important Rules

1. **App name must match directory:** `App(name="app")` (don't change this)
2. **Don't change the model** unless you know what you're doing
3. **Tool functions must:**
   - Have type hints
   - Return JSON-serializable dict
   - Have clear docstrings (sent to LLM)
4. **Test before deploying:** Always run `agents-cli eval run` first

## 🆘 Need Help?

- Run `agents-cli --help` for command reference
- Check `/Users/miapham06/Documents/Projects/agentic-cinema-hackathon/.adk-reference/` for detailed guides
- See `CLAUDE.md` for development workflow
- ADK Docs: https://adk.dev/
