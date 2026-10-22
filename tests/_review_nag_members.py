from teatree.types import RawAPIDict


def active_member(user_id: str, name: str) -> RawAPIDict:
    return {
        "id": user_id,
        "name": name,
        "deleted": False,
        "is_bot": False,
        "is_restricted": False,
        "is_ultra_restricted": False,
    }
