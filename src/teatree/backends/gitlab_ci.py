"""GitLab CI backend — fetch pipeline errors, test reports, trigger builds."""

import re
from typing import cast

from teatree.backends.gitlab_api import GitLabAPI


class GitLabCIService:
    def __init__(self, client: GitLabAPI | None = None) -> None:
        self._client = client or GitLabAPI()

    def fetch_pipeline_errors(self, *, project: str, ref: str) -> list[str]:
        project_info = self._client.resolve_project(project)
        if project_info is None:
            return [f"Could not resolve project: {project}"]

        pipeline = self._latest_pipeline(project_info.project_id, ref)
        if pipeline is None:
            return [f"No pipeline found for ref: {ref}"]

        pipeline_id = int(cast("int | str", pipeline["id"]))
        jobs = self._client.get_json(f"projects/{project_info.project_id}/pipelines/{pipeline_id}/jobs?per_page=100")
        if not isinstance(jobs, list):
            return []

        errors: list[str] = []
        for job in jobs:
            if job.get("status") != "failed":
                continue
            job_id = int(cast("int | str", job["id"]))
            trace = self._get_job_trace(project_info.project_id, job_id)
            if trace:
                errors.append(f"Job {job.get('name', job_id)} failed:\n{_extract_error_tail(trace)}")
        return errors

    def fetch_failed_tests(self, *, project: str, ref: str) -> list[str]:
        project_info = self._client.resolve_project(project)
        if project_info is None:
            return []

        pipeline = self._latest_pipeline(project_info.project_id, ref)
        if pipeline is None:
            return []

        pipeline_id = int(cast("int | str", pipeline["id"]))
        report = self._client.get_json(f"projects/{project_info.project_id}/pipelines/{pipeline_id}/test_report")
        if not isinstance(report, dict):
            return []

        failed: list[str] = []
        for suite in cast("list[dict[str, object]]", report.get("test_suites", [])):
            if not isinstance(suite, dict):
                continue
            for case in cast("list[dict[str, object]]", suite.get("test_cases", [])):
                if not isinstance(case, dict):
                    continue
                if case.get("status") == "failed":
                    classname = str(case.get("classname", ""))
                    name = str(case.get("name", ""))
                    node_id = f"{classname}::{name}" if classname else name
                    failed.append(node_id)
        return failed

    def cancel_pipelines(self, *, project: str, ref: str) -> list[int]:
        project_info = self._client.resolve_project(project)
        if project_info is None:
            return []
        return self._client.cancel_pipelines(project_info.project_id, ref)

    def trigger_pipeline(
        self,
        *,
        project: str,
        ref: str,
        variables: dict[str, str] | None = None,
    ) -> dict[str, object]:
        project_info = self._client.resolve_project(project)
        if project_info is None:
            return {"error": f"Could not resolve project: {project}"}

        payload: dict[str, object] = {"ref": ref}
        if variables:
            payload["variables"] = [{"key": k, "value": v, "variable_type": "env_var"} for k, v in variables.items()]

        result = self._client.post_json(
            f"projects/{project_info.project_id}/pipeline",
            payload,
        )
        return result or {}

    def quality_check(self, *, project: str, ref: str) -> dict[str, object]:
        project_info = self._client.resolve_project(project)
        if project_info is None:
            return {"error": f"Could not resolve project: {project}"}

        pipeline = self._latest_pipeline(project_info.project_id, ref)
        if pipeline is None:
            return {"error": f"No pipeline found for ref: {ref}"}

        pipeline_id = int(cast("int | str", pipeline["id"]))
        report = self._client.get_json(f"projects/{project_info.project_id}/pipelines/{pipeline_id}/test_report")
        if not isinstance(report, dict):
            return {"pipeline_id": pipeline_id, "status": pipeline.get("status")}

        return {
            "pipeline_id": pipeline_id,
            "status": pipeline.get("status"),
            "total_count": report.get("total_count", 0),
            "success_count": report.get("success_count", 0),
            "failed_count": report.get("failed_count", 0),
            "error_count": report.get("error_count", 0),
        }

    def _latest_pipeline(self, project_id: int, ref: str) -> dict[str, object] | None:
        data = self._client.get_json(f"projects/{project_id}/pipelines?ref={ref}&per_page=1")
        if isinstance(data, list) and data:
            return data[0]
        return None

    def _get_job_trace(self, project_id: int, job_id: int) -> str:
        import httpx  # noqa: PLC0415

        if not self._client.token:
            return ""
        response = httpx.get(
            f"{self._client.base_url}/projects/{project_id}/jobs/{job_id}/trace",
            headers={"PRIVATE-TOKEN": self._client.token},
            timeout=15.0,
        )
        if response.is_success:
            return response.text
        return ""


def _extract_error_tail(trace: str, *, max_lines: int = 50) -> str:
    lines = trace.splitlines()
    error_lines: list[str] = []
    in_error = False
    for line in lines:
        if re.search(r"(FAILED|ERROR|error:|Error:|assert|AssertionError|raise )", line):
            in_error = True
        if in_error:
            error_lines.append(line)
            if len(error_lines) >= max_lines:
                break
    if error_lines:
        return "\n".join(error_lines)
    return "\n".join(lines[-max_lines:])
