from agent_core.skills.loader import SkillDefinition, load_skill, load_skills_from_dir
from agent_core.skills.registry import SkillRegistry as WorkflowRegistry

# Workflows use the exact same format and loader as skills.
# The only distinction is the .workflow.md filename suffix and storage location.
WorkflowDefinition = SkillDefinition

__all__ = ["WorkflowDefinition", "WorkflowRegistry", "load_skill", "load_skills_from_dir"]
