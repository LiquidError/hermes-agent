from unittest.mock import patch

from agent import prompt_builder
from run_agent import AIAgent


def _make_agent_with_tools(tool_names):
    agent = AIAgent.__new__(AIAgent)
    agent.skip_context_files = True
    agent.valid_tool_names = set(tool_names)
    agent._memory_store = None
    agent._memory_manager = None
    agent._memory_enabled = False
    agent._user_profile_enabled = False
    agent.model = "gpt-4o"
    agent._tool_use_enforcement = "auto"
    agent._cached_system_prompt = None
    agent.ephemeral_system_prompt = None
    agent.pass_session_id = False
    agent.session_id = None
    agent.provider = None
    agent.platform = None
    return agent


def test_widget_guidance_appended_when_render_widget_present():
    agent = _make_agent_with_tools({"render_widget"})
    with patch("run_agent.load_soul_md", return_value=""):
        prompt = agent._build_system_prompt()
    assert prompt_builder.WIDGET_AUTHOR_GUIDANCE in prompt


def test_widget_guidance_absent_when_render_widget_missing():
    agent = _make_agent_with_tools({"memory"})
    with patch("run_agent.load_soul_md", return_value=""):
        prompt = agent._build_system_prompt()
    assert prompt_builder.WIDGET_AUTHOR_GUIDANCE not in prompt
