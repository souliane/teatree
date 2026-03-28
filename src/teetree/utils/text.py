def camelize(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("_"))
