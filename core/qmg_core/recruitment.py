"""Identify plays that need to verify the whole recruitment sequence."""


def contains_recruitment(steps):
    for step in steps:
        op = step.get("operation", step)
        effect = op.get("effect", op)
        if effect["type"] in {"RECRUIT", "recruit", "recruit-at-battle-space", "recruit-at-removed-space"}:
            return True
        if effect["type"] == "choose-space" and effect.get("action") == "recruit":
            return True
        if effect["type"] == "choose-one" and any(contains_recruitment(row["effects"]) for row in effect["options"]):
            return True
    return False


def recruitment_play(rules, definition, command):
    if command["type"] == "USE_STATUS":
        return contains_recruitment(rules.abilities[definition["active_ability"]]["effects"])
    return (command["type"] == "PLAY_CARD" and definition["type"] != "RESPONSE" and
            not command.get("play_as") and contains_recruitment(definition["effects"]))
