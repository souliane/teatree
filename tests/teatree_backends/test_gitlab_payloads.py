from teatree.backends.gitlab.payloads import mutation_errors, work_item_global_id, work_item_type_global_id


def test_work_item_global_id_extracts_gid() -> None:
    data = {"data": {"project": {"workItems": {"nodes": [{"id": "gid://gitlab/WorkItem/7"}]}}}}
    assert work_item_global_id(data) == "gid://gitlab/WorkItem/7"


def test_work_item_global_id_none_when_no_nodes() -> None:
    assert work_item_global_id({"data": {"project": {"workItems": {"nodes": []}}}}) is None


def test_work_item_global_id_none_when_project_null() -> None:
    assert work_item_global_id({"data": {"project": None}}) is None


def test_work_item_global_id_none_when_node_lacks_id() -> None:
    data = {"data": {"project": {"workItems": {"nodes": [{"iid": 7}]}}}}
    assert work_item_global_id(data) is None


def test_work_item_type_global_id_matches_case_insensitively() -> None:
    data = {
        "data": {
            "workspace": {
                "workItemTypes": {
                    "nodes": [
                        {"id": "gid://gitlab/WorkItems::Type/1", "name": "Issue"},
                        {"id": "gid://gitlab/WorkItems::Type/5", "name": "Task"},
                    ],
                },
            },
        },
    }
    assert work_item_type_global_id(data, "task") == "gid://gitlab/WorkItems::Type/5"


def test_work_item_type_global_id_none_when_absent() -> None:
    data = {"data": {"workspace": {"workItemTypes": {"nodes": [{"id": "g", "name": "Issue"}]}}}}
    assert work_item_type_global_id(data, "Epic") is None


def test_work_item_type_global_id_none_when_workspace_null() -> None:
    assert work_item_type_global_id({"data": {"workspace": None}}, "Task") is None


def test_mutation_errors_reads_field_errors() -> None:
    data = {"data": {"workItemConvert": {"errors": ["nope"]}}}
    assert mutation_errors(data, "workItemConvert") == ["nope"]


def test_mutation_errors_empty_when_no_errors() -> None:
    data = {"data": {"workItemUpdate": {"errors": []}}}
    assert mutation_errors(data, "workItemUpdate") == []


def test_mutation_errors_falls_back_to_top_level_graphql_errors() -> None:
    data = {"errors": [{"message": "syntax error"}, "bare string"]}
    assert mutation_errors(data, "workItemConvert") == ["syntax error", "bare string"]


def test_mutation_errors_empty_when_nothing_present() -> None:
    assert mutation_errors({"data": {}}, "workItemConvert") == []
