"""Sign the throwaway visual-QA server's browser in as its seeded operator."""

from collections.abc import Callable

from django.http import HttpRequest, HttpResponse

from e2e.dash.seed import visual_qa_operator


class ScratchOperatorLoginMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if not request.user.is_authenticated:
            request.user = visual_qa_operator()
        return self.get_response(request)
