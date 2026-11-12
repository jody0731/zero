"""Skills: folders of instructions for a kind of task, often with scripts
beside them, by the Agent Skills standard: a SKILL.md whose frontmatter names
the skill and says what it is for, its instructions after it.

found.py: where skills are found and what a SKILL.md holds; the door a
session finds them through (SkillFolders), which the assembly hands it.
hooks.py: the skills found, listed in the system message, each with its
author's description.
tool.py: read_skill, a skill's instructions in full, marked as its author's.

What a skill's text says, the model weighs as another's words. Its scripts
run as any command does, under the session's mode: a skill's own word on its
permissions (the standard's allowed-tools) is never read.
"""
